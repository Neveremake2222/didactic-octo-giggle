# Owl Memory System Architecture

> 文档版本：Phase 2 完成后（2026-04-16）
> 对应代码：`owl/` 目录下所有 memory 相关模块

---

## 一、总起 — 架构全景

### 1.1 系统定位

Owl 是一个基于 ReAct 架构的本地代码智能助手。记忆系统是它的"知识层"：负责在每次运行中感知环境、记录决策依据，并在跨轮次之间复用经验。

记忆系统的核心设计哲学：

> **memory 是仓库，context 是装配线**

- **记忆**（memory）：系统持有、未来可能被召回的东西
- **上下文**（context）：这一轮实际送进模型的东西
- **状态**（state）：控制面信息（当前步数、停止原因等），不属于记忆

### 1.2 架构总图

```
┌─────────────────────────────────────────────────────────────────┐
│                          ask() 循环                              │
│                                                                 │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────┐  │
│  │ ExecutionState │ → │ WorkingMemory│ → │  ContextBuilder     │  │
│  │  (控制状态)    │   │ (当前任务)    │   │  (prompt 组装)      │  │
│  └──────────────┘   └──────┬───────┘   └──────────┬─────────┘  │
│                             │                        ↓            │
│                      ┌──────┴───────┐         ┌──────────┐      │
│                      │ MemoryWriter │         │  Model   │      │
│                      │ (写入决策)   │         │          │      │
│                      └──────┬───────┘         └──────────┘      │
│                             │                        ↑            │
│                      ┌──────┴───────┐         ┌──────────┐      │
│                      │ MemoryRetriever│ ←───── │ ToolResult │      │
│                      │ (召回策略)    │         │ (工具结果) │      │
│                      └──────┬───────┘         └──────────┘      │
│                             │                                     │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │               MemoryCompactor (唯一桥梁)                  │    │
│  │  pre_compaction_flush → compact → promote → structured │    │
│  └──────────────────────────────────────────────────────────┘    │
│                             ↓                                     │
│  ┌──────────────┐   ┌─────────────────┐   ┌──────────────────┐  │
│  │ SemanticMemory│   │ SkillCandidate  │   │  ContextDiscovery │  │
│  │ (跨任务持久)   │   │ Registry       │   │  (局部上下文发现)  │  │
│  └──────────────┘   │ (程序性经验)    │   └──────────────────┘  │
│                      └─────────────────┘                          │
│  ┌──────────────┐   ┌─────────────────┐   ┌──────────────────┐  │
│  │ FileFingerprint│   │ ProcedureCand. │   │  RecallRanker    │  │
│  │ Tracker       │   │ Detector       │   │  (四维召回排序)  │  │
│  └──────────────┘   └─────────────────┘   └──────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 1.3 数据生命周期

```
工具执行结果
    ↓
MemoryWriter.should_write()  ← 审批：写不写？写哪层？什么格式？
    ↓
WorkingMemory.add_observation()  ← 当前任务的高相关动态内容
    ↓
StaleObservationGuard  ← 检查是否有文件已变化使观察过时
    ↓
MemoryCompactor.compact_and_promote_v2()  ← 四阶段处理
    ├─ pre_compaction_flush → CompactionSchema（快照骨架）
    ├─ compact_working_memory → 去重
    ├─ promote_to_semantic → 文件摘要沉淀
    └─ structured_compaction → 结构化总结沉淀
    ↓
SemanticMemory.put()  ← 跨任务持久化
    ↓
RecallRanker.rank()  ← 四维排序后召回
    ↓
MemoryRetriever.recall_for_task()  ← 供给 ContextBuilder
```

### 1.4 Phase 2 新增模块总览

| 方向 | 模块 | 功能 |
|------|------|------|
| A 上下文发现 | `context_sources.py` `context_discovery.py` `context_invalidation.py` | 从活跃文件向上遍历发现 AGENTS.md / README.md / rules，同次 run 去重 |
| B 结构化压缩 | `compaction_schema.py` | 定义两段式压缩的 schema，确保长任务骨架不漂移 |
| C 记忆有效性 | `memory_validity.py` `stale_observation_guard.py` | 文件指纹追踪、过期观察移除 |
| D 质量召回 | `recall_ranker.py` | relevance / freshness / importance / diversity 四维排序 + MMR 去重 |
| E 程序性经验 | `procedure_candidate_detector.py` `skill_candidate_registry.py` | 检测执行模式 → 四阶段晋升为 skill |

---

## 二、分述 — 各模块详解

### 2.1 ExecutionState（控制状态）

**文件**：`owl/execution_state.py`
**定位**：不属于记忆，属于"控制面"。在每次 `ask()` 开始时创建，结束时销毁。

这是整个系统知道自己"现在在哪"的机制。它不存储任务知识，只记录执行进度。

**核心字段**：

| 字段 | 含义 |
|------|------|
| `run_id` / `task_id` | 唯一标识 |
| `current_phase` | 当前阶段：initializing → prompt_building → model_calling → tool_executing → finished/stopped |
| `current_step` | 当前步数 |
| `step_budget` | 步数上限（默认 6） |
| `tool_attempts` | dict[tool_name] = 尝试次数 |
| `stop_reason` | 停止原因 |
| `failure_reason` | 失败原因 |

**关键方法**：
- `transition(phase)`：状态转换，同时 emit trace 事件
- `record_tool_call(tool_name)`：记录工具调用，递增步数
- `mark_stop(reason, failure_reason)`：标记停止

### 2.2 WorkingMemory（工作记忆）

**文件**：`owl/working_memory.py`
**定位**：当前任务最相关的高动态内容。生命周期仅限当前 `ask()` 调用，结束时由 compactor 决定哪些内容沉淀。

**设计原则**：只保留"当前任务最关键的一小撮信息"，不是完整历史的副本。

**Observation 数据类**：

| 字段 | 含义 |
|------|------|
| `tool_name` | 产生该观察的工具名 |
| `summary` | 工具结果的摘要（最多 500 字符） |
| `file_path` | 相关文件路径（Phase 2，用于指纹追踪） |
| `file_fingerprint` | 文件内容 SHA-256（Phase 2，用于过期检测） |

**WorkingMemory 五大字段**：

| 字段 | 上限 | 含义 |
|------|------|------|
| `plan` | 1 | 当前计划（下一步要做什么） |
| `recent_observations` | 8 | 最近工具结果摘要（FIFO 淘汰） |
| `active_hypotheses` | 4 | 当前假设（对问题的中间判断） |
| `candidate_targets` | 6 | 候选修改点（可能要改的文件/位置） |
| `pending_verifications` | 6 | 待验证事项（还需要确认的事情） |

**写入算法**：所有 list 字段使用相同的"去重→追加→截断"模式，确保最新内容优先保留：

```python
# 示例：add_hypothesis
if hypothesis in self.active_hypotheses:
    self.active_hypotheses.remove(hypothesis)  # 去重
self.active_hypotheses.append(hypothesis)       # 移到最后（最新）
if len(self.active_hypotheses) > MAX_HYPOTHESES:
    self.active_hypotheses = self.active_hypotheses[-MAX_HYPOTHESES:]
```

**渲染**：`render_text()` 将五大字段渲染为给模型阅读的紧凑文本，最后 4 条 observation 可见。

### 2.3 SemanticMemory（语义记忆）

**文件**：`owl/semantic_memory.py`
**定位**：跨任务持久化的长期记忆。只允许"跨任务可能复用、语义稳定"的信息写入。

**SemanticRecord 字段**：

| 字段 | 含义 | Phase 2 新增 |
|------|------|:---:|
| `record_id` | SHA-256(category:key) 生成的 12 位哈希 | |
| `category` | 记录类型：file_summary / run_goal / completed_work 等 | |
| `content` | 正文 | |
| `repo_path` | 关联的仓库路径 | |
| `tags` | 检索过滤标签 | |
| `source_run_id` | 来源 run ID | |
| `freshness_hash` | 内容 SHA-256，用于判断是否过期 | |
| `file_version` | 写入时的文件 SHA-256 | ✅ |
| `importance_score` | 0.0~1.0 重要性分，召回时参与排序 | ✅ |
| `superseded_by` | 替代该记录的新 record_id | ✅ |
| `invalidated_at` | 失效时间戳 | ✅ |

**有效性方法**：

```python
record.invalidate()        # 标记为已失效
record.supersede(new_id)   # 标记被新记录替代
record.is_active()         # 未失效且未替代 → True
```

**检索算法**：多维过滤 + token 重叠打分
1. category 精确匹配
2. tags 任一匹配
3. repo_path 精确匹配
4. query token（>2 字符）与 content/tag/path 任一匹配 → 命中

### 2.4 MemoryWriter（写入策略）

**文件**：`owl/memory_writer.py`
**定位**：统一写入审批层。**所有记忆写入必须经过此模块**，而不是直接裸写。

**三种写入目标**：

| 常量 | 含义 |
|------|------|
| `WRITE_TARGET_WORKING` | 写 WorkingMemory |
| `WRITE_TARGET_SEMANTIC` | 写 SemanticMemory |
| `WRITE_TARGET_SKIP` | 不写 |

**按工具类型的决策逻辑**：

| 工具 | 决策 | 原因 |
|------|-------|------|
| `read_file` | working + promote_to_semantic=True | 文件内容摘要值得长期保留 |
| `write_file` / `patch_file` | working + invalidate_old | 文件被修改，旧摘要需失效 |
| `list_files` / `search` | working | 导航结果有时效性 |
| `run_shell` | working | 命令结果观察 |
| `delegate` | working | 子 Agent 结果观察 |

**摘要生成**：工具结果摘要逻辑为取前 3 行非空非标题行，用 ` | ` 连接，截断到 180 字符。

**Phase 2 新增**：写入 SemanticMemory 时自动填充 `freshness_hash`（文件内容 SHA-256）和 `importance_score`。

### 2.5 MemoryRetriever（召回策略）

**文件**：`owl/memory_retriever.py`
**定位**：统一召回策略。**所有记忆召回必须经过此模块**，而不是全量拉回。

**RecallResult 字段**：

| 字段 | 含义 | Phase 2 新增 |
|------|------|:---:|
| `source` | 来源：working / semantic / episodic | |
| `content` | 召回内容 | |
| `relevance_score` | token 重叠相关性得分 | |
| `combined_score` | 四维加权总分 | ✅ |
| `freshness_score` | 新鲜度得分 | ✅ |
| `importance_score` | 重要性分 | ✅ |
| `recall_rationale` | 召回原因说明 | ✅ |

**召回流程**：
1. 从 WorkingMemory 召回（task_summary +1.0、observations +0.5、candidates +0.3 加权）
2. 从 SemanticMemory 召回（Phase 2 走 RecallRanker，Phase 1 回退到简单 token overlap）
3. 按 `combined_score` 降序排列
4. 返回 top_k

### 2.6 MemoryCompactor（压缩与沉淀）

**文件**：`owl/memory_compactor.py`
**定位**：WorkingMemory → SemanticMemory 的**唯一桥梁**。不允许任意原文直接沉淀进长期记忆。

**四阶段流程（`compact_and_promote_v2`）**：

```
阶段 1: pre_compaction_flush
    WorkingMemory ──→ CompactionSchema
    (快照骨架：原始请求 / 最终目标 / 已完成 / 剩余任务)

阶段 2: compact_working_memory
    去重：observations 按 summary 前 100 字符去重
    hypotheses 和 candidates 按顺序去重

阶段 3: promote_to_semantic
    文件被观察 >= 2 次 → 沉淀为 file_summary 记录
    (需满足：摘要非空、不含 error)

阶段 4: structured_compaction
    CompactionSchema ──→ SemanticMemory
    (按 category 拆分为 run_goal / completed_work /
     remaining_tasks / run_summary 多条记录)
```

### 2.7 CompactionSchema（压缩骨架）

**文件**：`owl/compaction_schema.py`
**定位**：两段式压缩的中间产物。定义了在压缩发生前必须快照的骨架字段。

**字段定义**：

| 字段 | 来源 |
|------|------|
| `original_request` | ask() 入口参数 |
| `final_goal` | `wm.plan` |
| `completed_work` | 含有 done/success/pass/fixed/updated 的 observations |
| `remaining_tasks` | `wm.pending_verifications` |
| `files_observed` | 从 observation summaries 提取的文件路径 |
| `hypotheses_tested` | 含 hypothesis 关键词的 observations |
| `summary_text` | goal + done + remaining 的自然语言合成 |

**关键设计**：Schema 到 SemanticRecord 的转换（`schema_to_semantic_records`）将每个字段按独立 category 写入，而非合并成一条记录。这样在长任务跨轮次时可以精确查询"剩余任务"或"已完成工作"。

### 2.8 RecallRanker（四维召回排序）

**文件**：`owl/recall_ranker.py`
**定位**：SemanticMemory 召回结果的精排引擎。

**四维打分公式**：

```
combined = 0.40 × relevance
         + 0.25 × freshness
         + 0.20 × importance
         + 0.15 × diversity
```

| 维度 | 算法 | 默认权重 |
|------|------|:--------:|
| relevance | Token 重叠率 = overlap / query_tokens | 0.40 |
| freshness | 指数衰减 = 0.5^(age_seconds / halflife)，半衰期 7 天 | 0.25 |
| importance | 直接读取 SemanticRecord.importance_score | 0.20 |
| diversity | MMR 惩罚：相似度 > 85%（Jaccard）→ diversity_score 降权 | 0.15 |

**MMR 去重流程**：
1. 按 combined_score 降序遍历候选项
2. 计算与已选项的最大 Jaccard 相似度
3. 若 > 85%，降低 diversity_score 并重新计算 combined
4. combined < 0.1 时跳过

**RecallReport 输出**：不仅返回排序结果，还包含 `total_candidates`、`deduplicated_count`、`stale_skipped_count`，供 trace 和分析使用。

### 2.9 MemoryValidity（记忆有效性）

**文件**：`owl/memory_validity.py`
**定位**：判断 SemanticRecord 是否仍然有效的规则引擎。

**FileFingerprintTracker**：内存中的 path → SHA-256(content) 索引。

**有效性判定规则（按优先级）**：

```
1. invalidated_at 已设置 → INVALIDATED → drop
2. superseded_by 已设置 → SUPERSEDED → drop
3. repo_path 存在 → 检查文件指纹是否变化
   ├── 变化 → STALE → refresh
   └── 未变 → VALID → keep
4. 无法判定 → VALID → keep
```

### 2.10 StaleObservationGuard（过期观察守护）

**文件**：`owl/stale_observation_guard.py`
**定位**：保护 WorkingMemory 中的观察不被过期文件内容污染。

**工作流程**：

```
工具执行后：
    遍历 recent_observations
        提取 file_path（优先读 obs.file_path，否则从 summary 解析）
        在 FileFingerprintTracker 中查找历史指纹
        读取当前文件内容，计算新指纹
        若不一致 → StaleObservation

移除时：
    按索引降序 pop（避免偏移问题）
```

### 2.11 ProcedureCandidateDetector（程序性经验检测）

**文件**：`owl/procedure_candidate_detector.py`
**定位**：从 WorkingMemory 中识别可复用的执行模式。

**三种检测模式**：

| 模式 | 条件 | confidence |
|------|------|:----------:|
| `repeated_file_access` | 同一文件被读 >= 3 次 | 0.60 |
| `hypothesis_verification_flow` | 同时有 hypothesis 和 pending_verification | 0.50 |
| `multi_step_completion` | >= 2 个 observations 含 fix/patch/update/wrote/success/done | 0.55 |

### 2.12 SkillCandidateRegistry（技能候选注册）

**文件**：`owl/skill_candidate_registry.py`
**定位**：管理程序性经验的晋升路径。

**四阶段晋升**：

```
semantic_fact (置信度 ≥ 0.70)
    ↓
procedure_candidate (置信度 ≥ 0.85)
    ↓
skill_candidate (置信度 ≥ 0.95)
    ↓
established_skill
```

**置信度调整**：
- 成功使用 → +0.05（成功强化）
- 失败使用 → -0.10（失败惩罚，不对称设计）
- 重复注册 → +0.15（多 run 验证）

### 2.13 ContextDiscovery（局部上下文发现）

**文件**：`owl/context_discovery.py`
**定位**：围绕当前活跃文件，向上遍历发现最近的 AGENTS.md / README.md / CONTRIBUTING.md 和规则文件，并注入 prompt。

**发现策略**：向上遍历最多 5 层祖先目录：

```
当前文件: src/utils/helper.py
    层 1: src/utils/AGENTS.md     ✓
    层 2: src/AGENTS.md           ✓
    层 3: src/README.md            ✓
    层 4: AGENTS.md               ✓
    层 5: .github/AGENTS.md       ✓
    层 6+: 不再向上
```

**Prompt 注入**：800 chars 预算均分给各来源，渲染格式为：

````markdown
### AGENTS.md: `src/AGENTS.md`
_概要: Project coding conventions_

[文件内容截断到 budget/len(sources) chars]
````

### 2.14 ContextSources / ContextInvalidation（来源追踪）

**文件**：`owl/context_sources.py` / `owl/context_invalidation.py`

**ContextSource**：发现结果的数据载体，包含 source_id、absolute_path、content、header、fingerprint、category。

**ContextInjectedTracker**：同次 run 内去重器。`mark_injected(source)` 首次返回 True，再次返回 False，防止同一来源被重复注入 prompt。

**ContextFingerprintIndex**：来源文件的内容指纹索引，与 MemoryValidity 的 FileFingerprintTracker 平行，维护"注入时"的指纹快照。

### 2.15 Legacy LayeredMemory（遗留模块）

**文件**：`owl/memory.py`
**定位**：Phase 1 前的原始记忆实现。与新的 WorkingMemory/SemanticMemory 并存，尚未完全废弃。

**设计差异**：
- 函数式 API（所有方法接受并返回 state dict） vs 新的 OOP dataclass
- 手工 tokenization（正则 `[A-Za-z0-9_]+`） vs 简单 split
- 无分层记忆结构
- 仍有部分 trace 和 benchmark 代码依赖此模块

---

## 三、总结 — 设计原则与数据流

### 3.1 五大核心设计原则

| 原则 | 含义 | 体现模块 |
|------|------|---------|
| **写入审批制** | 所有写记忆必须经过 MemoryWriter 审批 | `memory_writer.py` 唯一写入入口 |
| **唯一桥梁** | WorkingMemory → SemanticMemory 必须经过 Compactor | `memory_compactor.py` 唯一桥梁 |
| **骨架优先** | 压缩前先快照骨架，防止长任务漂移 | `compaction_schema.py` |
| **无盲召** | 所有召回必须经过 Retriever，不允许全量拉回 | `memory_retriever.py` |
| **失效显式** | 文件变化 → 指纹追踪 → 观察失效，不静默污染 | `memory_validity.py` + `stale_observation_guard.py` |

### 3.2 Feature Flag 切换策略

所有 Phase 2 模块均可通过 feature flag 关闭，回退 Phase 1 行为：

```python
DEFAULT_FEATURE_FLAGS = {
    # Phase 1
    "memory": True,
    "relevant_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
    "workspace_refresh": True,
    # Phase 2
    "context_discovery": True,       # 局部上下文发现
    "structured_compaction": True,    # 两段式压缩
    "memory_validity": True,         # 记忆有效性
    "stale_guard": True,             # 过期观察守护
    "quality_recall": True,          # 质量感知召回
    "procedure_detection": True,     # 程序性经验检测
}
```

### 3.3 Trace 事件全景

```
ask() 开始
  └─ run_started
       └─ state_transition (initializing)

  循环内（每步）
       ├─ state_transition (prompt_building)
       ├─ context_built          ← ContextSnapshot
       ├─ context_sources_discovered  ← Phase 2 A
       ├─ model_requested
       ├─ model_parsed
       ├─ tool_executed
       ├─ memory_written
       │    └─ memory_skipped_stale  ← Phase 2 C
       ├─ state_transition (tool_executing)

  ask() 结束
       ├─ precompaction_flushed    ← Phase 2 B
       ├─ context_compacted        ← Phase 2 B
       ├─ compaction_promoted     ← Phase 2 B
       ├─ procedure_candidates_detected  ← Phase 2 E
       ├─ run_finished
```

### 3.4 路径提取的统一问题

当前有四处重复实现了相同的"从 observation summary 中提取文件路径"逻辑：

1. `memory_compactor.py::_extract_path_from_observation()`
2. `stale_observation_guard.py::_extract_path_from_summary()`
3. `compaction_schema.py::_extract_path_from_summary()`
4. `procedure_candidate_detector.py::_extract_path()`

未来应统一为一个共享工具函数，放置在 `owl/_path_utils.py` 或 `memory_utils.py` 中。

### 3.5 待优化项

| 问题 | 优先级 | 说明 |
|------|:------:|------|
| 路径提取逻辑重复 | 中 | 四处重复实现 |
| LayeredMemory 尚未废弃 | 低 | 与新模块并存，维护成本 |
| SemanticMemory 无持久化 | 高 | 仅内存存储，重启丢失 |
| 无 embedding 向量检索 | 中 | 当前仅 token 重叠，精度受限 |
| 跨 Session 的 Skill 晋升未持久化 | 中 | SkillCandidateRegistry 也在内存中 |

---

## 附录：模块文件清单

```
owl/
├── memory.py                    # Legacy LayeredMemory（遗留）
├── execution_state.py           # 控制状态
├── working_memory.py            # 工作记忆
├── semantic_memory.py           # 语义记忆
├── memory_writer.py             # 写入策略
├── memory_retriever.py          # 召回策略
├── memory_compactor.py          # 压缩与沉淀
├── compaction_schema.py         # 压缩骨架定义
├── memory_validity.py          # 记忆有效性
├── stale_observation_guard.py  # 过期观察守护
├── recall_ranker.py            # 四维召回排序
├── procedure_candidate_detector.py  # 程序性经验检测
├── skill_candidate_registry.py # 技能候选注册
├── context_sources.py           # 上下文来源类型
├── context_discovery.py         # 局部上下文发现
└── context_invalidation.py      # 上下文去重与指纹
```
