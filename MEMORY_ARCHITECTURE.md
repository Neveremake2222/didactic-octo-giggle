# Owl Memory Architecture(记忆模块介绍)  :)

> Updated: 2026-04-16  
> Scope: current repository implementation in `owl/`  
> Status: architecture document aligned with the current codebase

---

## 1. 文档说明

这份文档从五个维度介绍 Owl 当前的记忆系统实现：

1. 记忆模块的结构
2. 记忆模块的作用
3. 记忆系统的工作原理
4. 记忆系统的评价标准
5. 记忆系统当前的表现

这份文档描述的是当前仓库里已经落地的实现，而不是抽象目标设计。

---

## 2. 设计目标

Owl 的记忆系统服务于一个非常具体的问题：让本地 coding agent 在真实代码仓库里连续工作时，不因为上下文窗口限制、任务跨度变长或文件状态变化而快速“失忆”。

因此这套设计主要解决五类问题：

1. 当前任务中的观察和推理过程如何在多步工具调用之间保留下来。
2. 一次运行中得到的有效信息，如何被压缩成后续可复用的长期知识。
3. 历史知识如何在新任务里被准确召回，而不是把无关旧信息一并带进 prompt。
4. 当文件已经变化时，旧记忆如何被识别为过期，避免污染当前推理。
5. 记忆效果如何被持续评估，而不是只靠主观感觉判断“似乎更聪明了”。

围绕这些目标，当前系统采用了分层记忆架构：

`Tool Result -> WorkingMemory -> MemoryCompactor -> SemanticMemory`

---

## 3. 记忆模块的结构

## 3.1 总体结构

当前记忆系统可以分成五层：

```text
Layer 0: Legacy Compatibility
  - owl/memory.py
  - 保留旧的 session["memory"]、notes、files 等结构
  - 主要用于兼容旧测试、旧会话、旧调用路径

Layer 1: Working Memory
  - owl/working_memory.py
  - 保存当前 run 内的短期状态

Layer 2: Compaction / Promotion
  - owl/memory_compactor.py
  - 负责 working memory 到 semantic memory 的正式提升

Layer 3: Semantic Memory
  - owl/semantic_memory.py
  - 负责跨 run 的长期持久化存储

Layer 4: Recall / Context
  - owl/memory_retriever.py
  - owl/recall_ranker.py
  - owl/context_manager.py
  - 负责召回、排序、过滤和 prompt 组装
```

## 3.2 主执行链路

在运行时，记忆主链路如下：

```text
用户请求
  -> Owl.ask()
  -> 模型/工具循环
  -> Tool Result
  -> MemoryWriter.write_working()
  -> WorkingMemory
  -> MemoryCompactor.compact_and_promote_v2()
  -> SemanticMemory (.owl/memory/semantic-memory.db)
  -> MemoryRetriever.recall_for_task()
  -> RecallRanker.rank()
  -> ContextManager.build()
  -> prompt
```

## 3.3 当前核心对象结构

### WorkingMemory

`WorkingMemory` 当前主要保存：

- `task_summary`
- `plan`
- `recent_observations`
- `active_hypotheses`
- `candidate_targets`
- `pending_verifications`

其中 observation 已经引入 `observation_id`，用于稳定删除与 stale 清理。

### SemanticMemory

`SemanticMemory` 当前使用 repo 级 SQLite 持久化，默认落盘到：

```text
.owl/memory/semantic-memory.db
```

`SemanticRecord` 的关键字段包括：

- `record_id`
- `category`
- `content`
- `repo_path`
- `file_path`
- `file_version`
- `freshness_hash`
- `importance_score`
- `invalidated_at`
- `superseded_by`

这意味着长期记忆不再只是“内存里的概念层”，而是具备真实生命周期管理的 repo 级知识库。

## 3.4 与结构配套的辅助模块

为了让这套结构真正可运行，当前代码还引入了三个关键辅助模块：

- `owl/memory_validity.py`
  负责文件指纹记录、语义记录有效性校验、失效过滤。
- `owl/stale_observation_guard.py`
  负责 working memory 中陈旧 observation 的检测与移除。
- `owl/memory_config.py`
  负责集中收敛记忆相关阈值、限制和排序参数。

---

## 4. 记忆模块的作用

## 4.1 `owl/working_memory.py`

作用：承载当前任务的短期工作状态。

它不是长期知识库，而是当前 run 的“思考台面”，主要保存：

- 刚刚通过工具拿到的事实
- 当前正在形成的判断与假设
- 当前可能要修改的文件和目标
- 还没验证完成的待确认事项

它的价值在于保证模型在多步工具调用之间，不需要每一轮都重新读取所有上下文。

## 4.2 `owl/memory_writer.py`

作用：统一记忆写入入口。

当前设计里，所有工具结果不会直接任意写入长期记忆，而是先经过 `MemoryWriter` 判定：

- 该不该写
- 写成什么类别
- 先进入 working memory 还是跳过

这使得记忆写入从“分散副作用”变成“显式策略动作”，降低多写入路径带来的不一致。

## 4.3 `owl/memory_compactor.py`

作用：负责 working memory 的压缩、清理与长期提升。

它是当前架构里 working -> semantic 的正式桥梁，主要承担：

- 去重
- 结构化压缩
- 总结提炼
- 过程信息转长期知识
- procedure candidate 检测

它的意义在于把“运行时噪声”变成“后续任务可复用知识”，避免长期记忆被大量原始 observation 直接淹没。

## 4.4 `owl/semantic_memory.py`

作用：保存跨运行可复用的长期知识。

这部分记忆主要存储：

- 某个文件或模块的重要事实
- 已验证的结构性认识
- 可以跨轮复用的经验性知识
- 被压缩后的长期 summary

同时它还承担生命周期管理：

- active
- invalidated
- superseded

也就是说，长期记忆不是只会“越积越多”，而是可以被失效、替换和过滤。

## 4.5 `owl/memory_retriever.py`

作用：按任务相关性召回 working memory 和 semantic memory。

它负责把“仓库里已有的记忆”转化成“当前任务真正需要的记忆候选集”，避免全量拉回。

## 4.6 `owl/recall_ranker.py`

作用：对召回结果进行质量排序。

当前排序维度包括：

- relevance
- freshness
- importance
- diversity

这一步的目标不是简单“找出包含相同关键词的记录”，而是优先把更相关、更新鲜、更重要、且不重复的记忆送入 prompt。

## 4.7 `owl/context_manager.py`

作用：把记忆真正装入 prompt。

当前 section 顺序为：

```text
prefix
memory
relevant_memory
history
current_request
```

它会结合预算限制，对 memory、relevant memory 和 history 做裁剪与压缩，避免上下文全部被历史信息挤满。

## 4.8 `owl/memory_validity.py`

作用：保证记忆引用的文件状态仍然有效。

它通过文件指纹、版本信息和 freshness hash，让系统可以判断某条长期记忆是否仍对应当前文件状态。

## 4.9 `owl/stale_observation_guard.py`

作用：清理 working memory 中已经过期的 observation。

如果文件在 observation 记录后又被修改，这个模块会尝试识别并移除陈旧 observation，减少旧事实对当前判断的误导。

---

## 5. 记忆系统的原理

## 5.1 原理一：短期记忆与长期记忆分层

WorkingMemory 与 SemanticMemory 的职责被显式拆开：

- `WorkingMemory` 只负责当前 run 内的高相关动态状态
- `SemanticMemory` 只负责跨 run 可复用的长期知识

这样做的核心原因是两者的生命周期、噪声容忍度和使用方式完全不同。

如果把两者混在一起，就会出现两个问题：

1. 当前 run 的临时推理噪声污染长期知识
2. 长期知识过多反过来淹没当前任务的即时上下文

## 5.2 原理二：长期记忆只能通过压缩链路生成

当前架构中，长期记忆的正式来源是：

`WorkingMemory -> MemoryCompactor -> SemanticMemory`

这意味着长期记忆不鼓励直接写入，而是先经过观察、筛选、去重和结构化提炼后再进入持久层。

它的收益是：

- 单一事实来源更清晰
- 长期记忆质量更稳定
- 更容易解释一条语义记忆是如何形成的

## 5.3 原理三：召回不是“有就全拿”，而是“先找、再排、再裁”

当前 recall 原理分三步：

1. `MemoryRetriever` 先从 working/semantic 中找候选
2. `RecallRanker` 根据相关性、时效性、重要性、多样性排序
3. `ContextManager` 再根据 prompt 预算做裁剪和注入

这让记忆系统更像一个“检索与组装系统”，而不是“历史堆叠系统”。

## 5.4 原理四：文件状态变化必须反向影响记忆有效性

代码仓库场景和普通聊天场景最大的不同在于：文件内容会不断变化。

因此 Owl 记忆系统有一个关键原则：

> 记忆不能只考虑“曾经是否正确”，还必须考虑“现在是否仍然成立”。

围绕这个原则，当前实现引入了：

- `FileFingerprintTracker`
- `SemanticRecordValidityChecker`
- `StaleObservationGuard`

对应到实际流程：

- `read_file` 后记录文件指纹
- `write_file` / `patch_file` 后触发 invalidation
- recall 时过滤 stale / invalidated / superseded record

## 5.5 原理五：上下文预算必须被显式管理

记忆系统的目标不是“尽量多塞信息”，而是“在有限预算下提供最有用的信息”。

因此当前实现把以下内容都纳入预算管理：

- memory section 长度
- relevant memory section 长度
- history section 长度
- section floor / section limit
- reduction order

这保证了 prompt 构建过程可解释、可裁剪、可追踪，而不是不可控增长。

## 5.6 原理六：记忆系统必须可观测

一套真实可维护的记忆系统，不能只靠最终任务是否完成来判断优劣。

因此当前实现把记忆相关行为纳入：

- `trace.jsonl`
- `report.json`
- `metrics`
- benchmark artifact

并为记忆写入、记忆召回、记忆排序、stale skip 等行为定义了可跟踪事件。

---

## 6. 评价标准

记忆系统的评价不能只看“有没有 memory 模块”，而要看它是否真正提升了 agent 的任务执行质量与稳定性。当前可以从五类标准评估。

## 6.1 结构正确性

关注点：

- 写入链路是否单一
- 长短期记忆是否职责清晰
- 召回入口是否统一
- 参数是否集中配置

当前主要观察项：

- 是否以 `MemoryWriter` 作为写入入口
- 是否以 `MemoryCompactor` 作为 working -> semantic 的正式桥梁
- 是否以 `MemoryRetriever` 作为 recall 主入口
- 关键阈值是否收敛到 `owl/memory_config.py`

## 6.2 召回质量

关注点：

- 是否能召回正确记忆
- 是否减少无关记忆进入 prompt
- 是否避免重复、过期或冲突记忆污染当前任务

当前可用指标：

- `selected_count`
- `relevant_memory` 命中情况
- `repeated_reads`
- `repeated_identical_call_count`
- invalidated / stale 过滤情况

对应代码与产物：

- `owl/memory_retriever.py`
- `owl/recall_ranker.py`
- `report.json`
- memory experiments

## 6.3 上下文质量

关注点：

- prompt 长度是否稳定
- relevant memory 是否先于 history 被合理裁剪
- 当前请求是否始终保留
- 历史信息是否保留关键线索而不是全部原样堆叠

当前可用指标：

- `prompt_chars`
- `memory_chars`
- `relevant_selected_count`
- section budgets / reductions metadata

对应代码与产物：

- `owl/context_manager.py`
- `owl/context_builder.py`
- `context_built` trace event
- `report.json`

## 6.4 运行稳定性

关注点：

- 多步任务中是否减少重复读文件
- 是否降低 no-progress loop
- 文件变更后是否正确失效旧记忆
- 测试环境与真实运行是否隔离

当前可用指标：

- `repeated_reads`
- `no_progress_loop_count`
- `premature_done`
- semantic DB 隔离情况
- stale cleanup 行为

对应代码与产物：

- `owl/stale_observation_guard.py`
- `owl/memory_validity.py`
- `tests/conftest.py`
- `trace.jsonl`

## 6.5 可解释性与可观测性

关注点：

- 记忆何时写入
- 记忆何时召回
- 为什么某条记忆进入 prompt
- trace 是否能完整讲清一次 run 的记忆行为

当前可用指标：

- `memory_written`
- `memory_recalled`
- `memory_ranked`
- `memory_skipped_stale`
- trace completeness / order validity

对应代码与产物：

- `owl/trace_schema.py`
- `owl/trace_validator.py`
- `trace.jsonl`
- `report.json`

---

## 7. 当前表现

## 7.1 架构层面的表现

与改造前相比，当前记忆系统已经表现出几个明显变化：

1. 主链路已经收敛为 `Tool Result -> WorkingMemory -> MemoryCompactor -> SemanticMemory`，长期记忆写入不再是多路散射。
2. `SemanticMemory` 已经是 repo 级 SQLite 持久化，而不是仅存在于进程内。
3. recall 由新链路主导，legacy memory 主要退回兼容层角色。
4. stale observation cleanup 与 semantic validity filtering 已接入真实运行链路，不再是孤立组件。
5. 关键阈值已集中到 `owl/memory_config.py`，调参与维护成本明显降低。

## 7.2 运行时表现

从当前代码实现看，记忆系统已经具备以下运行时能力：

- 工具执行后能够把高价值 observation 写入 WorkingMemory
- 运行结束后能够进行压缩、去重与长期提升
- 后续任务能够从 semantic memory 中按相关性召回长期知识
- 文件变化后能够对相关语义记忆做 invalidation
- prompt 组装时能够优先注入 relevant memory，并控制上下文预算

这意味着 Owl 的记忆系统已经不只是“保存历史文本”，而是形成了完整的写入、提炼、召回、过滤和注入闭环。

## 7.3 验证与测试表现

当前仓库内可直接确认的验证结果包括：

- memory 重构相关代码回归测试已通过：`312 passed, 1 skipped`
- 固定 benchmark 工件 `benchmarks/benchmark-v1.json` 已记录：
  - `task_count = 6`
  - `pass_rate = 1.0`

这些结果至少说明两件事：

1. 当前记忆相关重构没有破坏基础 benchmark 与回归测试。
2. 记忆链路、上下文链路与运行工件输出已经能在固定任务集上稳定工作。

## 7.4 评测能力上的表现

除了已有通过结果，当前代码还已经具备继续评估 memory 的基础设施：

- fixed regression benchmark
- behavioral benchmark
- failure benchmark
- trace completeness / order validation
- small-scale memory experiment
- large-scale memory experiment
- memory experiments v2

当前代码中已经明确把以下指标作为 memory 效果评估项：

- `pass_rate`
- `prompt_chars`
- `avg_tool_steps`
- `repeated_reads`
- `relevant_selected_count`
- `trace_completeness_rate`
- `memory_event_rate`

这说明系统不仅实现了记忆模块，也已经把“如何证明记忆有效”纳入工程体系。

## 7.5 当前仍然存在的边界

虽然当前表现已经明显优于重构前，但仍存在几个现实边界：

- legacy compatibility layer 仍未完全移除
- 某些场景下 `ContextManager` 仍保留 legacy recall 回退逻辑
- 当前长期检索主要还是基于现有 token / SQLite 机制，尚未引入更强语义检索能力
- memory experiments 的部分结果在代码中已具备框架，但不是每次提交都自动生成新的实验工件

因此，当前阶段更准确的判断是：

> Owl 的记忆系统主干架构已经成型，具备真实可运行、可验证、可维护的工程状态，但仍处在“持续收敛兼容层与增强评测”的阶段。

---

## 8. 文件映射

与记忆系统直接相关的核心文件如下：

- `owl/runtime.py`
- `owl/working_memory.py`
- `owl/semantic_memory.py`
- `owl/memory_writer.py`
- `owl/memory_retriever.py`
- `owl/memory_compactor.py`
- `owl/context_manager.py`
- `owl/context_builder.py`
- `owl/memory_validity.py`
- `owl/stale_observation_guard.py`
- `owl/recall_ranker.py`
- `owl/procedure_candidate_detector.py`
- `owl/memory_config.py`
- `owl/memory_utils.py`
- `owl/trace_schema.py`
- `owl/trace_validator.py`
- `owl/metrics.py`

用于验证和隔离测试环境的配套文件：

- `tests/conftest.py`
- `tests/test_memory_new_modules.py`
- `tests/test_pico.py`
- `tests/test_safety_invariants.py`
- `benchmarks/benchmark-v1.json`
- `benchmarks/refactor_behavior_v1.json`
- `benchmarks/refactor_failure_v1.json`

---

## 9. 一句话总结

Owl 当前的记忆系统，本质上是一套围绕真实代码仓库场景设计的分层记忆架构：它把当前任务状态、长期知识沉淀、文件有效性校验、召回排序和 prompt 预算控制组合成一条完整闭环，使 agent 在多步工程任务中具备更强的上下文保持、经验复用、失效过滤和可解释评估能力。
