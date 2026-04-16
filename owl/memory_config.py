"""Memory 子系统的全局配置常量。

所有 memory 相关常量必须在此文件中定义并导出。
不得在任何其他文件中重新定义这些常量。

常量来源：
  - RELEVANT_MEMORY_LIMIT: 从 context_manager.py / context_builder.py 移入
  - MAX_OBSERVATIONS / MAX_HYPOTHESES / MAX_CANDIDATES / MAX_PENDING: 从 working_memory.py 移入
  - MIN_OBSERVATIONS_FOR_PROMOTION: 从 memory_compactor.py 移入
  - recall 权重: 从 recall_ranker.py 移入
  - MIN_TOKEN_LEN: 从 memory_utils.py 移入
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Prompt 组装常量（来自 context_manager.py / context_builder.py）
# ---------------------------------------------------------------------------

RELEVANT_MEMORY_LIMIT = 3  # 每次召回的相关笔记条数上限

# ---------------------------------------------------------------------------
# Working Memory 上限（来自 working_memory.py）
# ---------------------------------------------------------------------------

MAX_OBSERVATIONS = 8
MAX_HYPOTHESES = 4
MAX_CANDIDATES = 6
MAX_PENDING = 6

# ---------------------------------------------------------------------------
# 沉淀条件（来自 memory_compactor.py）
# ---------------------------------------------------------------------------

MIN_OBSERVATIONS_FOR_PROMOTION = 2  # 文件摘要沉淀所需最少观察次数

# ---------------------------------------------------------------------------
# Recall / Ranking 权重（来自 recall_ranker.py）
# ---------------------------------------------------------------------------

DEFAULT_FRESHNESS_HALFLIFE = 7 * 24 * 3600  # 衰减半衰期：7 天

DEFAULT_MMR_LAMBDA = 0.3  # MMR 参数（0 = 只看多样性，1 = 只看相关性）

DEFAULT_WEIGHTS = {
    "relevance": 0.40,
    "freshness": 0.25,
    "importance": 0.20,
    "diversity": 0.15,
}

SIMILARITY_THRESHOLD = 0.85  # 相似度阈值（>85% 视为重复）

# ---------------------------------------------------------------------------
# Tokenization（来自 memory_utils.py）
# ---------------------------------------------------------------------------

MIN_TOKEN_LEN = 2  # 最短有效 token 长度
