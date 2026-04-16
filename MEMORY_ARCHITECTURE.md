# Owl Memory Architecture

> Updated: 2026-04-16  
> Scope: current repository implementation in `owl/`  
> Status: aligned with the refactor work that has been implemented and verified by test

---

## 1. 文档目的

这份文档描述 Owl 当前的记忆系统真实实现，而不是仅描述目标设计。

重点说明四件事：

1. 这次 memory 重构前，架构上存在哪些问题。
2. 这次修改后，核心链路变成了什么样。
3. 当前各模块分别负责什么。
4. 仍然保留了哪些兼容层，后续还能往哪里继续收敛。

---

## 2. 改造前的问题

在这轮重构前，memory 相关实现有几个典型问题：

### 2.1 长短期记忆链路不够单一

理论目标应该是：

`Tool Result -> WorkingMemory -> MemoryCompactor -> SemanticMemory`

但旧实现里同时存在：

- runtime 直接写 legacy `LayeredMemory`
- `MemoryWriter` 既参与 working memory，又可能直接写 semantic memory
- prompt 组装时混用 legacy memory 和新 memory

结果是：

- 单一事实来源不清晰
- 很难判断一条记忆到底来自哪里
- 测试经常被“同一信息被多路写入”干扰

### 2.2 SemanticMemory 跨运行持久化不稳定

旧设计里 long-term memory 的目标是跨 run 复用，但运行时并没有稳定地把语义记忆绑定到 repo 级数据库。

典型后果：

- 新开进程后长期记忆不一定还在
- 不同测试之间容易共用同一个 repo-root semantic DB
- 测试和真实运行行为不一致

### 2.3 文件有效性和陈旧检测链路不完整

虽然已经有：

- `FileFingerprintTracker`
- `SemanticRecordValidityChecker`
- `StaleObservationGuard`

但旧链路里这些组件没有完全嵌入真实工具执行流：

- `read_file` 后不一定记录指纹
- `write_file` / `patch_file` 后不一定马上触发失效
- recall 时不能稳定过滤 stale semantic record

### 2.4 Prompt 组装混合新旧 recall

`ContextManager` 旧实现会同时依赖：

- legacy `LayeredMemory.retrieval_candidates()`
- 新的 `MemoryRetriever.recall_for_task()`

后果：

- prompt 中 relevant memory 来源不透明
- 很难解释为什么当前这条记忆会出现在 prompt
- 历史压缩和 memory metadata 也不够稳定

### 2.5 参数和规则分散

旧实现里很多 memory 相关阈值直接散落在不同文件中，比如：

- relevant memory 条数
- observation 上限
- recall rank 权重
- similarity threshold

后续调参成本高，也不利于维护。

---

## 3. 改造后的总体架构

当前记忆架构可以概括为：

### 3.1 主链路

```text
用户请求
  -> Owl.ask()
  -> 模型/工具循环
  -> MemoryWriter 只写 WorkingMemory
  -> 运行结束时由 MemoryCompactor 统一压缩和提升
  -> SemanticMemory 持久化到 .owl/memory/semantic-memory.db
  -> 后续 run 通过 MemoryRetriever 召回
  -> ContextManager 组装到 prompt
```

### 3.2 当前分层

```text
Layer 0: Legacy Compatibility
  - owl/memory.py
  - 继续保留 session["memory"] / files / notes 等旧结构
  - 主要用于兼容旧测试、旧会话、旧接口

Layer 1: Working Memory
  - owl/working_memory.py
  - 存本轮运行内的短期状态
  - 包括 observations / hypotheses / candidate targets / pending verifications

Layer 2: Compaction / Promotion
  - owl/memory_compactor.py
  - 负责 working -> semantic 的唯一正式桥梁
  - 同时做去重、总结、structured compaction、procedure candidate detection

Layer 3: Semantic Memory
  - owl/semantic_memory.py
  - repo 级 SQLite 持久化
  - 支持 file_path / file_version / freshness_hash / invalidation / supersede

Layer 4: Recall / Context
  - owl/memory_retriever.py
  - owl/recall_ranker.py
  - owl/context_manager.py
  - 负责召回、排序、过滤、压缩后进入 prompt
```

---

## 4. 改造前后对比

| 维度 | 改造前 | 改造后 |
|---|---|---|
| 长期记忆写入 | 可能被多条路径直接写入 | 以 `MemoryCompactor` 为正式提升通道 |
| 短期记忆 | legacy state 与新结构并存，但职责混杂 | `WorkingMemory` 作为本轮短期状态主载体 |
| 长期存储 | 不够稳定，跨运行行为不统一 | repo 级 SQLite：`.owl/memory/semantic-memory.db` |
| Recall 来源 | legacy recall 与新 recall 混用 | 新 recall 为主，legacy 作为兼容回退 |
| 文件失效检测 | 有组件但接入不完整 | 指纹、失效、stale 清理、validity checker 已接入主流程 |
| Prompt metadata | 对 recall 来源解释不够充分 | relevant/history metadata 明确记录 recall item 和压缩行为 |
| 参数管理 | 多处散落 | 收敛到 `owl/memory_config.py` |

---

## 5. 当前真实执行流

## 5.1 Runtime 初始化

核心位置：`owl/runtime.py`

当前 `Owl.__init__()` 会初始化：

- `self.memory`: legacy `LayeredMemory`
- `self.working_memory`: 新 WorkingMemory
- `self.semantic_memory`: 新 SemanticMemory
- `self._memory_writer`
- `self._memory_retriever`
- `self._memory_compactor`
- `self.context_manager`

其中 `SemanticMemory` 当前绑定：

```text
.owl/memory/semantic-memory.db
```

这意味着同一 repo 下的不同 run 可以共享长期记忆。

## 5.2 ask() 主流程

`Owl.ask()` 当前关键步骤如下：

1. 更新任务摘要到 memory。
2. 初始化 `TaskState` 和 `ExecutionState`。
3. 重建本轮 `WorkingMemory`。
4. 按 feature flag 初始化：
   - context discovery
   - fingerprint tracker
   - stale guard
   - semantic validity checker
5. 每轮循环中：
   - `ContextManager.build()` 组装 prompt
   - 模型返回 tool 或 final
   - tool 执行后：
     - `update_memory_after_tool()` 维护 legacy 兼容 memory
     - `MemoryWriter.should_write()`
     - `MemoryWriter.write_working()`
     - 文件修改时 semantic invalidation
     - stale observation cleanup
6. 成功或停止时：
   - `MemoryCompactor.compact_and_promote_v2()`
   - structured compaction
   - procedure candidate detection
   - 写 trace / report / metrics

---

## 6. 关键模块职责

## 6.1 `owl/working_memory.py`

职责：本轮运行内短期记忆。

当前结构包括：

- `task_summary`
- `plan`
- `recent_observations`
- `active_hypotheses`
- `candidate_targets`
- `pending_verifications`

这次重构后的重要变化：

- observation 增加 `observation_id`
- stale removal 不再依赖列表索引，而是依赖稳定 ID
- `render_text()` 输出统一为 `Memory:` 开头，方便 prompt 组装
- observation / hypothesis / candidate / pending 上限改由 `memory_config.py` 管理

这解决了两个问题：

- stale 清理时不会因为索引变化误删
- prompt memory section 与 legacy 文本格式更接近

## 6.2 `owl/semantic_memory.py`

职责：跨运行长期记忆。

当前实现要点：

- 默认 SQLite backend
- 数据库文件为 `.owl/memory/semantic-memory.db`
- 支持 WAL
- 支持 `put` / `put_many` / `get` / `search` / `invalidate_by_file` / `delete`
- 支持 active / invalidated / superseded 生命周期

当前 `SemanticRecord` 关键字段：

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
- `created_at`
- `updated_at`

这次重构后的关键变化：

- `file_path` 成为一等字段，而不是只靠 `repo_path`
- 支持文件粒度失效
- 支持 active record 查询
- 支持 SQLite 与内存 fallback 的一致接口

## 6.3 `owl/memory_writer.py`

职责：工具执行后把信息写入短期记忆。

当前定位：

- `should_write()` 负责决策
- `write_working()` 负责写入 `WorkingMemory`
- `write_semantic()` 仍然存在，但更像兼容层 / 辅助接口

当前决策类型主要包括：

- `observation`
- `file_summary`
- `file_modified`

当前实际 runtime 行为是：

- 工具执行后主写入路径是 working memory
- semantic promotion 主要通过 compactor 完成

也就是说，writer 不再是长期记忆的主要桥梁。

## 6.4 `owl/memory_compactor.py`

职责：把短期记忆整理成长期可复用知识。

当前包含两条能力线：

### A. 旧式 compact + promote

- `compact_working_memory()`
- `promote_to_semantic()`
- `compact_and_promote()`

### B. 新式 structured compaction

- `pre_compaction_flush()`
- `structured_compaction()`
- `compact_and_promote_v2()`

`compact_and_promote_v2()` 现在是 runtime 结束阶段的核心入口。

它做四件事：

1. 从 `WorkingMemory` 生成 `CompactionSchema`
2. 对 working memory 去重
3. 把足够稳定的 file summary 提升到 semantic memory
4. 写入 run-level structured semantic records

另外它还集成：

- `ProcedureCandidateDetector`

即在 run 结束时检测是否出现可以沉淀为 procedure/skill 的行为模式。

## 6.5 `owl/memory_retriever.py`

职责：统一 recall。

当前会从两类来源召回：

- `WorkingMemory`
- `SemanticMemory`

返回统一的 `RecallResult`，其中包含：

- `source`
- `content`
- `repo_path`
- `relevance_score`
- `combined_score`
- `freshness_score`
- `importance_score`
- `recall_rationale`
- `metadata`

当前 recall 行为：

- working memory 结果优先作为本轮上下文
- semantic memory 结果可走 quality-aware 排序
- recall 前可接入 validity checker 过滤 stale semantic records

## 6.6 `owl/recall_ranker.py`

职责：对 semantic recall 做质量排序。

当前排序考虑：

- relevance
- freshness
- importance
- diversity

并通过 `memory_config.py` 中的参数统一控制：

- freshness half-life
- MMR lambda
- weight 配置
- similarity threshold

## 6.7 `owl/memory_validity.py`

职责：文件级有效性判断。

当前两个核心类：

- `FileFingerprintTracker`
- `SemanticRecordValidityChecker`

### FileFingerprintTracker

维护：

- `path -> fingerprint`
- `alias -> resolved path`

支持：

- `record()`
- `update()`
- `check()`
- `check_from_file()`

### SemanticRecordValidityChecker

对 semantic record 做四类判断：

- `INVALIDATED`
- `SUPERSEDED`
- `STALE`
- `VALID`

这让 semantic recall 可以基于真实文件状态过滤旧知识。

## 6.8 `owl/stale_observation_guard.py`

职责：在 run 内清理已经过期的 working observations。

当前流程：

1. 从 observation 的 `file_path` 或 summary 中提取路径
2. 通过 `FileFingerprintTracker` 检查当前文件是否已变化
3. 构造 `StaleObservation`
4. 用 `observation_id` 精确删除对应 observation

这次修改的关键点是：

- 从“按索引删除”改为“按 observation_id 删除”

这是一个很重要的稳定性修复。

## 6.9 `owl/context_manager.py`

职责：把 prefix / memory / relevant_memory / history / current_request 组装成 prompt。

当前 section 顺序：

```text
prefix
memory
relevant_memory
history
current_request
```

当前实现比旧版更清楚的地方：

- 支持注入 `MemoryRetriever`
- 支持 working / semantic memory source
- relevant memory metadata 更完整
- history 支持：
  - 旧 `read_file` 重复读取压缩
  - 旧 tool output 摘要化
  - recent entries 保留更多信息

另外，为了兼容旧行为，当前还保留了一个回退逻辑：

- 如果新 recall 只给出 trivial working-memory echo，而 legacy episodic recall 更有价值，则允许回退 legacy recall

这是一种“以新架构为主，但不牺牲已有行为”的折中方案。

## 6.10 `owl/memory_config.py`

职责：集中管理 memory 相关阈值。

当前已统一的参数包括：

- `RELEVANT_MEMORY_LIMIT`
- `MAX_OBSERVATIONS`
- `MAX_HYPOTHESES`
- `MAX_CANDIDATES`
- `MAX_PENDING`
- `MIN_OBSERVATIONS_FOR_PROMOTION`
- `DEFAULT_FRESHNESS_HALFLIFE`
- `DEFAULT_MMR_LAMBDA`
- `DEFAULT_WEIGHTS`
- `SIMILARITY_THRESHOLD`
- `MIN_TOKEN_LEN`

这解决了“配置分散”的问题。

---

## 7. 当前的数据流

## 7.1 读文件

```text
read_file
  -> tool result
  -> MemoryWriter.should_write(category=file_summary)
  -> MemoryWriter.write_working()
  -> WorkingMemory.add_observation(...)
  -> FileFingerprintTracker.record(...)
  -> 结束阶段由 MemoryCompactor 决定是否提升为 SemanticRecord
```

## 7.2 改文件

```text
write_file / patch_file
  -> tool result
  -> MemoryWriter.should_write(category=file_modified)
  -> MemoryWriter.write_working()
  -> FileFingerprintTracker.update(...)
  -> SemanticMemory.invalidate_by_file(path)
  -> StaleObservationGuard 清理基于旧文件内容的 working observations
```

## 7.3 Recall

```text
用户请求
  -> ContextManager.build()
  -> MemoryRetriever.recall_for_task()
  -> WorkingMemory recall
  -> SemanticMemory search
  -> SemanticRecordValidityChecker 过滤 stale / invalidated / superseded
  -> RecallRanker 排序
  -> relevant_memory section
```

## 7.4 Run 结束

```text
run finished
  -> MemoryCompactor.compact_and_promote_v2()
  -> pre_compaction_flush
  -> compact_working_memory
  -> promote_to_semantic
  -> structured_compaction
  -> procedure candidate detection
  -> trace / report / metrics
```

---

## 8. Feature Flags

当前 `owl/runtime.py` 中默认开启的 memory 相关 feature：

```python
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "relevant_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
    "workspace_refresh": True,
    "context_discovery": True,
    "structured_compaction": True,
    "memory_validity": True,
    "stale_guard": True,
    "quality_recall": True,
    "procedure_detection": True,
}
```

含义可以概括为：

- `memory`: 是否启用记忆系统
- `relevant_memory`: 是否做 recall 注入
- `structured_compaction`: 是否启用新 compaction 主路径
- `memory_validity`: 是否启用 semantic record validity 检查
- `stale_guard`: 是否启用 working observation stale 清理
- `quality_recall`: 是否启用 recall ranker
- `procedure_detection`: 是否在结束阶段检测 procedure candidate

---

## 9. 兼容层与当前取舍

当前还没有完全删除 legacy memory，而是采取了兼容保留策略。

保留原因：

- 旧 session 结构仍依赖 `session["memory"]`
- 旧测试仍依赖 `files` / `notes` / `file_summaries`
- 一些 prompt 行为还需要 legacy recall 兜底

因此当前是“双层并存，但职责已更清晰”的状态：

- 新架构负责真实 memory pipeline
- legacy layer 负责兼容旧接口和旧测试

这比改造前要好很多，因为现在：

- working / semantic 已经有清晰职责
- prompt 主要走新 context manager
- semantic store 已经可持久化
- stale / validity 已经真正接入运行时

---

## 10. 与本次代码修改直接对应的文件

这轮 memory 架构更新直接涉及的核心文件：

- `owl/runtime.py`
- `owl/working_memory.py`
- `owl/semantic_memory.py`
- `owl/memory_writer.py`
- `owl/memory_retriever.py`
- `owl/memory_compactor.py`
- `owl/context_manager.py`
- `owl/memory_validity.py`
- `owl/stale_observation_guard.py`
- `owl/recall_ranker.py`
- `owl/procedure_candidate_detector.py`
- `owl/memory_config.py`
- `owl/memory_utils.py`

为保证测试环境和 repo 隔离，还补充修改了：

- `tests/conftest.py`
- `pyproject.toml`

这些配套改动保证：

- pytest 使用仓库内临时目录
- semantic DB 在测试中按 case 隔离
- clone 到别的环境后，测试行为更稳定、更可复制

---

## 11. 验证状态

本轮 memory 架构相关代码已经完成一次完整回归验证。

当前测试结果：

```text
312 passed, 1 skipped
```

唯一 skip 为：

- Windows 无 symlink 权限时跳过 symlink 安全测试

这属于环境权限差异，不属于 memory 架构故障。

---

## 12. 后续仍可继续优化的方向

虽然当前架构已经明显比旧实现稳定，但仍有进一步收敛空间：

### 12.1 彻底移除 legacy recall 混用

当前 `ContextManager` 仍允许在某些场景下回退 legacy episodic recall。

后续理想状态：

- 所有 relevant memory 都来自 `MemoryRetriever`
- legacy 只保留 session 兼容，不再参与 prompt 决策

### 12.2 让 `MemoryWriter.write_semantic()` 彻底退场

当前语义上已经由 compactor 主导，但代码层面接口仍保留。

后续可以继续收敛成：

- writer 只负责 working
- compactor 唯一负责 semantic

### 12.3 进一步增强 semantic search

当前 SQLite search 已经可用，但后续还可以继续增强：

- 更细粒度 tags 过滤
- 更稳定的 path-aware tokenization
- 更强的 duplicate suppression

### 12.4 继续减少 runtime God Class 压力

虽然 memory 主链路已经拆出来了，但 `owl/runtime.py` 仍然承担了较多 orchestration 责任。

后续可以继续拆：

- tool execution coordinator
- memory pipeline coordinator
- prompt pipeline coordinator

---

## 13. 一句话总结

这次 memory 架构更新，本质上完成了三件关键事情：

1. 把短期记忆、长期记忆、压缩提升、召回过滤这几层真正拆开了。
2. 把 semantic memory 从“概念上的长期记忆”变成了“repo 级可持久化长期记忆”。
3. 把 stale detection、validity check、prompt recall、测试隔离都接进了真实运行链路。

因此，当前 Owl 的 memory 系统已经从“新旧方案混杂的半重构状态”，进入了“主干架构已成型、兼容层仍在但边界更清晰”的阶段。
