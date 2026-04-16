"""Memory 系统公共工具函数。

本模块整合了原本分散在多个文件中的重复逻辑：
  - 统一 tokenization 策略
  - 统一路径提取
  - 统一文件摘要生成
  - 统一相关性打分

所有 memory 相关模块应从此导入，禁止各自实现重复逻辑。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 常量（从 memory_config 集中导入）
# ---------------------------------------------------------------------------

from .memory_config import (
    MIN_TOKEN_LEN,
    SIMILARITY_THRESHOLD,
)

# 常见编程语言和配置文件后缀（用于路径提取）
FILE_EXTENSIONS = (".py", ".md", ".txt", ".json", ".yaml", ".yml",
                   ".toml", ".ini", ".cfg", ".conf", ".sh", ".bash",
                   ".zsh", ".html", ".css", ".js", ".ts", ".jsx",
                   ".tsx", ".go", ".rs", ".java", ".c", ".cpp", ".h",
                   ".hpp", ".cs", ".rb", ".php", ".sql", ".proto")


# ---------------------------------------------------------------------------
# 统一 Tokenization
# ---------------------------------------------------------------------------

def tokenize(text: str) -> set[str]:
    """将文本 tokenize 为小写词集合（最小长度 2）。

    统一策略：按空白符分割，忽略长度 <= 2 的词。
    覆盖 semantic_memory / memory_retriever / recall_ranker / memory_writer
    等模块的一致性需求。

    注意：legacy memory.py 使用正则表达式 [A-Za-z0-9_]+，
    两者行为略有不同（新代码用空白分割，更适合短文本摘要）。
    """
    return {t.lower() for t in str(text).split() if len(t) > MIN_TOKEN_LEN}


def tokenize_legacy(text: str) -> set[str]:
    """遗留 tokenization：使用正则保留连字符和下划线。

    供 legacy memory.py 中的 retrieval_candidates 兼容使用。
    新代码统一使用 tokenize()。
    """
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_]+", str(text))}


# ---------------------------------------------------------------------------
# 统一相关性计算
# ---------------------------------------------------------------------------

def compute_relevance(text: str, query: str) -> float:
    """计算 text 相对于 query 的 token overlap 得分。

    返回 0.0~1.0，表示 query 中有多少 token 出现在 text 中。
    完全不匹配返回 0.0；query 所有 token 都命中返回 1.0。

    公式：overlap / len(query_tokens)
    """
    query_tokens = tokenize(query)
    if not query_tokens:
        return 0.0
    text_tokens = tokenize(text)
    if not text_tokens:
        return 0.0
    overlap = len(query_tokens & text_tokens)
    return min(overlap / len(query_tokens), 1.0)


# ---------------------------------------------------------------------------
# 统一 Jaccard 相似度
# ---------------------------------------------------------------------------

def compute_similarity(text1: str, text2: str) -> float:
    """计算两段文本的 Jaccard 相似度。

    用于 recall_ranker 的 MMR 去重阶段。
    返回 0.0~1.0，越高表示越相似。
    """
    tokens1 = tokenize(text1)
    tokens2 = tokenize(text2)
    if not tokens1 or not tokens2:
        return 0.0
    intersection = len(tokens1 & tokens2)
    union = len(tokens1 | tokens2)
    return intersection / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# 统一路径提取
# ---------------------------------------------------------------------------

def extract_path_from_text(text: str) -> str:
    """从任意文本（observation summary / note text）中提取文件路径。

    支持格式：
      1. "read path/to/file: summary" 格式（最优先匹配）
      2. 含 "/" 的路径片段
      3. 含已知后缀的文件名

    去除了 `[](),.:` 等常见标点符号的包裹。

    Returns:
        提取到的路径字符串，若无法提取则返回空字符串。
    """
    text = str(text).strip()

    # 格式1: "read path/to/file: summary"
    if ":" in text and text.startswith("read "):
        parts = text.split(":", 1)
        path = parts[0].replace("read ", "").strip()
        if path:
            return path

    # 格式2/3: 含路径分隔符或已知后缀的词
    for word in text.split():
        # 优先识别含 / 的路径
        if "/" in word:
            clean = word.strip("[]():,.")
            if clean:
                return clean
        # 其次识别含已知后缀的词
        if any(word.lower().endswith(ext) for ext in FILE_EXTENSIONS):
            clean = word.strip("[]():,.")
            if clean:
                return clean

    return ""


def extract_path_from_observation(obs: Any) -> str:
    """从 observation 对象提取文件路径。

    优先取 obs.file_path，否则从 obs.summary 提取。
    兼容任意具有 summary 属性或可转为字符串的对象。
    """
    # 优先取结构化字段
    file_path = getattr(obs, "file_path", "")
    if file_path:
        return str(file_path)

    # 回退到 summary
    summary = getattr(obs, "summary", "")
    if summary:
        path = extract_path_from_text(summary)
        if path:
            return path

    # 最后尝试字符串化
    return extract_path_from_text(str(obs))


# ---------------------------------------------------------------------------
# 统一摘要生成
# ---------------------------------------------------------------------------

def summarize_result(result: str, limit: int = 180) -> str:
    """对工具结果生成短摘要。

    策略：取前 3 个非空行（跳过 Markdown 一级标题），
    用 " | " 连接，总长度不超过 limit。

    用于：
      - memory_writer._summarize_result()
      - memory.update_memory_after_tool() 中的摘要

    这是经过验证的摘要策略，能在极短空间内传达最多信息。
    """
    lines = [line.strip() for line in str(result).splitlines() if line.strip()]
    if not lines:
        return "(empty)"
    # 跳过 Markdown 一级标题（常见于文件开头）
    if lines[0].startswith("# "):
        lines = lines[1:]
    if not lines:
        return "(empty)"
    summary = " | ".join(lines[:3])
    # 确保不超过 limit
    if len(summary) > limit:
        summary = summary[:limit - 3] + "..."
    return summary


# ---------------------------------------------------------------------------
# 文件指纹
# ---------------------------------------------------------------------------

def file_fingerprint(path: str) -> str:
    """计算文件内容的 SHA-256 fingerprint。

    文件不存在或无法读取时返回空字符串。
    """
    try:
        content = Path(path).read_text(encoding="utf-8")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    except (OSError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------------------------
# 统一 ID 生成
# ---------------------------------------------------------------------------

def make_record_id(category: str, key: str, length: int = 12) -> str:
    """生成稳定的短 ID。

    默认取 SHA-256 前 12 位，与 SemanticMemory.make_record_id 兼容。
    可指定 length 用于不同场景。
    """
    raw = f"{category}:{key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


# ---------------------------------------------------------------------------
# 时间戳解析（供 legacy memory.py 复用）
# ---------------------------------------------------------------------------

def parse_timestamp(value: str) -> float:
    """将 ISO timestamp 字符串解析为 Unix timestamp（秒）。"""
    if not value:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return 0.0
