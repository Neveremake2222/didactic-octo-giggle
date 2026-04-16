"""本地仓库上下文发现引擎。

根据当前活跃的文件路径，沿目录向上遍历发现 AGENTS.md / README.md /
CONTRIBUTING.md / 规则文件，并将结果注入 prompt。

发现策略：向上遍历祖先目录（最多 MAX_ANCESTOR_WALK 层），每层检查文档
文件名和规则目录。结果按 source_id 去重（首次发现优先）。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .context_sources import (
    CANONICAL_DOC_FILES,
    RULE_DIRS,
    RULE_FILE_NAMES,
    ContextSource,
)

# 最大向上遍历层数
MAX_ANCESTOR_WALK = 5

# prompt 中注入该类内容的最大字符数
DEFAULT_INJECT_BUDGET = 800


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_header(content: str) -> str:
    """提取首行非注释文字。"""
    lines = content.splitlines()
    for line in lines:
        stripped = line.strip()
        # 跳过空行、Markdown 标题、注释
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("<!--") or stripped.startswith("*/"):
            continue
        # 截断到 100 字符
        return stripped[:100]
    return ""


def _classify(filename: str) -> str:
    """根据文件名分类。"""
    name = filename.lower()
    if name == "agents.md":
        return "AGENTS.md"
    if name == "readme.md":
        return "README.md"
    if name == "contributing.md":
        return "CONTRIBUTING.md"
    if name in RULE_FILE_NAMES or any(name.endswith(r.lower()) for r in RULE_FILE_NAMES):
        return "rule_file"
    return "other"


def _build_source(
    repo_root: Path,
    file_path: Path,
    discovered_from: str,
) -> ContextSource | None:
    """从给定路径构建 ContextSource（如果它是合法来源文件）。"""
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    filename = file_path.name
    category = _classify(filename)
    if category == "other":
        return None

    # 计算 repo 相对路径（统一使用正斜杠）
    try:
        rel = str(file_path.relative_to(repo_root)).replace("\\", "/")
    except ValueError:
        rel = str(file_path).replace("\\", "/")

    return ContextSource(
        source_id=rel,
        absolute_path=str(file_path),
        discovered_from=discovered_from,
        content=content,
        header=_extract_header(content),
        fingerprint=_sha256(content),
        category=category,
    )


class ContextDiscovery:
    """本地仓库上下文发现引擎。

    使用方式：
      discovery = ContextDiscovery(workspace_root="/path/to/repo")
      sources = discovery.discover_for_file("/path/to/repo/src/main.py")
      sources = discovery.discover_for_paths(["/path/to/repo/src/a.py", "/path/to/repo/src/b.py"])
      text = discovery.render_for_prompt(sources)
    """

    def __init__(self, workspace_root: str):
        self.repo_root = Path(workspace_root).resolve()

    # -------------------------------------------------------------------------
    # 发现入口
    # -------------------------------------------------------------------------

    def discover_for_file(self, active_file_path: str) -> list[ContextSource]:
        """从单个活跃文件出发，向上遍历发现上下文来源。

        最多向上走 MAX_ANCESTOR_WALK 层，每层检查：
          1. CANONICAL_DOC_FILES 中的文件名
          2. RULE_DIRS 中的文件

        返回去重后的 ContextSource 列表（按发现顺序）。
        """
        seen_ids: set[str] = set()
        results: list[ContextSource] = []

        try:
            current = Path(active_file_path).resolve()
        except Exception:
            return results

        # 向上遍历到 repo_root 为止
        ancestor_count = 0
        while current != current.parent and ancestor_count < MAX_ANCESTOR_WALK:
            # 重置到当前祖先目录（不含文件本身）
            current = current.parent
            ancestor_count += 1

            # 检查文档文件
            for doc_name in CANONICAL_DOC_FILES:
                candidate = current / doc_name
                if not candidate.is_file():
                    continue
                source = _build_source(self.repo_root, candidate, active_file_path)
                if source is None:
                    continue
                if source.source_id in seen_ids:
                    continue
                seen_ids.add(source.source_id)
                results.append(source)

            # 检查规则目录
            for rule_dir in RULE_DIRS:
                rule_path = current / rule_dir
                if not rule_path.exists():
                    continue
                if rule_path.is_file():
                    # 直接是文件，如 ".github/AGENTS.md"
                    source = _build_source(self.repo_root, rule_path, active_file_path)
                    if source and source.source_id not in seen_ids:
                        seen_ids.add(source.source_id)
                        results.append(source)
                elif rule_path.is_dir():
                    # 目录下可能含多个 .md 规则文件
                    for rule_file in rule_path.glob("*.md"):
                        source = _build_source(self.repo_root, rule_file, active_file_path)
                        if source and source.source_id not in seen_ids:
                            seen_ids.add(source.source_id)
                            results.append(source)

        return results

    def discover_for_paths(self, paths: list[str]) -> list[ContextSource]:
        """从多个路径出发，合并去重发现上下文来源。

        对每个路径调用 discover_for_file()，结果按 source_id 全局去重。
        """
        seen_ids: set[str] = set()
        results: list[ContextSource] = []

        for path in paths:
            for source in self.discover_for_file(path):
                if source.source_id in seen_ids:
                    continue
                seen_ids.add(source.source_id)
                results.append(source)

        return results

    # -------------------------------------------------------------------------
    # Prompt 渲染
    # -------------------------------------------------------------------------

    def render_for_prompt(
        self,
        sources: list[ContextSource],
        budget_chars: int = DEFAULT_INJECT_BUDGET,
    ) -> str:
        """将多个 ContextSource 渲染为一段注入 prompt 的文本。

        每条来源格式：
          ## {category}: {source_id}
          {header}
          {content 截断到 budget/len(sources) chars}
        """
        if not sources:
            return ""

        lines = ["## Local Context Sources\n"]
        per_source_budget = max(budget_chars // len(sources), 100)

        for source in sources:
            # 元信息行
            lines.append(f"### {source.category}: `{source.source_id}`")
            if source.header:
                lines.append(f"_概要: {source.header}_")

            # 内容（截断）
            content = source.content
            if len(content) > per_source_budget:
                content = content[:per_source_budget].rstrip() + "\n... (truncated)"

            lines.append(content)
            lines.append("")  # 空行分隔

        return "\n".join(lines).strip()

    # -------------------------------------------------------------------------
    # 注入辅助
    # -------------------------------------------------------------------------

    def inject_into_prompt(
        self,
        prompt: str,
        context_text: str,
        marker: str = "## Local Context Sources",
    ) -> str:
        """将 context_text 注入 prompt 的系统信息区域末尾。

        如果 context_text 非空，在系统信息末尾（紧跟 workspace 描述后）插入。
        """
        if not context_text:
            return prompt
        return f"{prompt.rstrip()}\n\n{context_text}"
