# Owl Benchmark Design

## 1. 设计结论

当前仓库里的 `benchmarks/coding_tasks.json` 应继续保留，但它的定位应明确为：

- 固定回归集
- 基础正确性检查
- 轻量流程稳定性检查

它不能单独证明上下文与记忆重构已经成功，因为现有任务：

- 任务数少，只有 6 个
- 难度低，几乎都是单文件文本替换
- 工具空间小，只覆盖 `read_file` 和 `patch_file`
- 多数 run 只有 `1` 次工具调用
- 默认依赖脚本化 `FakeModelClient`

因此新的评测体系应拆成两层：

1. `Regression Benchmark`
   - 保留现有固定任务，持续防回归
2. `Refactor Evaluation Suite`
   - 新增专门验证上下文、记忆、停止原因和 trace 的评测任务与实验

---

## 2. 评测目标

新的设计必须能回答以下 6 个问题：

1. `prompt` 长度是否更稳定
2. 无关上下文是否减少
3. 重复工具调用是否下降
4. `stop reason` 是否更清晰
5. 长期记忆是否更少噪声
6. `trace` 是否更容易解释一次运行

这些目标里，`3/4/6` 适合直接进 benchmark；`1/2/5` 需要 benchmark 和专项 experiment 结合证明。

---

## 3. 总体设计

### 3.1 分层结构

建议把评测分成 3 个层次：

| 层次 | 目的 | 是否保留现有方案 | 主要输出 |
|------|------|------------------|----------|
| L1: Fixed Regression | 防止基础功能退化 | 是 | pass rate, verifier pass |
| L2: Behavioral Benchmark | 验证多步行为和失败分类 | 新增 | process/outcome/trace 指标 |
| L3: Diagnostics Experiments | 验证上下文压缩、记忆噪声、稳定性 | 扩展现有 metrics | 专项统计报告 |

### 3.2 文件布局建议

建议新增如下结构：

```text
benchmarks/
  coding_tasks.json                  # 现有固定回归集，保留
  refactor_behavior_v1.json          # 新的行为型 benchmark
  refactor_failure_v1.json           # 新的失败/停止原因 benchmark

tests/fixtures/
  bench_v2_context_noise/
  bench_v2_multistep_edit/
  bench_v2_failure_modes/
  bench_v2_memory_conflict/
  bench_v2_trace_cases/

scripts/
  run_refactor_eval.py               # 统一跑 L2 + L3

artifacts/eval/
  <experiment-name>/
    benchmark-artifacts/
    metrics/
    reports/
```

---

## 4. 现有 Benchmark 的新定位

`benchmarks/coding_tasks.json` 保留，但只承担以下职责：

- 文件修改能力未退化
- verifier 机制正常
- 基本 stop reason 仍然可落在 `final_answer_returned`
- 跑批流程、artifact 输出、report 输出未退化

不再把它作为以下问题的主要证据：

- 上下文噪声是否减少
- prompt 是否更稳定
- 长期记忆是否更可靠
- trace 是否足够解释复杂运行

---

## 5. 新增 Behavioral Benchmark 设计

## 5.1 设计原则

新 benchmark 任务应满足：

- 至少需要 `2-4` 步工具调用
- 至少覆盖 `read_file`、`search`、`patch_file`、必要时 `run_shell`
- 任务不能靠单次 patch 直接完成
- verifier 不只检查最终文件，还要检查行为过程指标
- 部分任务必须故意失败，用来验证 `stop reason`

## 5.2 建议的最小任务集

建议先做 `12` 个任务，足够覆盖 6 个目标。

### A. 上下文与噪声任务 `4` 个

1. `context_latest_override`
- 场景：历史里有旧约束，当前请求里有新约束
- 目标：验证 agent 不被旧历史带偏
- verifier：最终修改必须遵从“最新请求”
- 观察指标：`prompt_chars`、`relevant_memory.selected_count`、错误引用旧约束次数

2. `context_irrelevant_memory_filter`
- 场景：工作记忆里塞入 1 条相关事实和 8 条无关事实
- 目标：验证无关记忆不过度进入 prompt
- verifier：依赖相关事实完成修改
- 观察指标：无关记忆进入 prompt 的占比、压缩率、相关记忆命中率

3. `context_long_history_keep_target`
- 场景：长对话历史中只有一段与当前文件相关
- 目标：验证历史裁剪后仍保留关键线索
- verifier：能定位正确目标文件并完成修改
- 观察指标：`history_chars`、`budget_reductions`、是否出现误改

4. `context_multi_file_disambiguation`
- 场景：多个文件有相似字段，只有一个应修改
- 目标：验证上下文不混淆目标
- verifier：仅目标文件被改动
- 观察指标：候选目标数量、误改率、重复读文件次数

### B. 多步编排与重复调用任务 `4` 个

5. `workflow_search_read_patch`
- 场景：必须先搜索，再读，再改
- 目标：验证基本多步编排
- verifier：修改正确且步骤完整
- 观察指标：`tool_call_count`、`unique_tool_count`

6. `workflow_verify_before_done`
- 场景：修改后需要读回或运行 verifier 才能结束
- 目标：验证不会过早 `Done`
- verifier：必须通过回读验证
- 观察指标：`premature_done`、`tool_steps`

7. `workflow_avoid_duplicate_read`
- 场景：文件首次读取后，第二轮回答可直接依赖记忆
- 目标：验证重复读取下降
- verifier：答案正确
- 观察指标：`repeated_identical_call_count`、`repeated_reads`

8. `workflow_partial_signal_recovery`
- 场景：第一次搜索结果不完整，需要二次定位但不能陷入循环
- 目标：验证恢复能力
- verifier：最终仍能完成任务
- 观察指标：`no_progress_loop_count`、`attempts`

### C. 停止原因任务 `4` 个

9. `stop_reason_step_limit`
- 场景：刻意给不足的 `step_budget`
- 目标：验证 `step_limit_reached`
- verifier：停止原因必须精确匹配

10. `stop_reason_retry_limit`
- 场景：模型多次返回不可执行动作
- 目标：验证 `retry_limit_reached`
- verifier：停止原因必须精确匹配

11. `stop_reason_policy_block`
- 场景：任务需要一个被策略阻止的工具
- 目标：验证 `approval_denied` 或等价 stop/failure 分类
- verifier：停止与失败分类必须正确

12. `stop_reason_model_error`
- 场景：模型层抛出异常或返回非法格式
- 目标：验证 `model_error`
- verifier：错误归类必须稳定

---

## 6. 长期记忆专项实验设计

长期记忆噪声不建议仅靠 benchmark 验证，建议单独保留为 experiment。

## 6.1 目标

验证以下能力：

- 旧知识是否可召回
- 新知识是否能覆盖旧知识
- 无关知识是否不会污染召回
- 冲突知识是否按时间或优先级被正确处理

## 6.2 建议实验组

### 实验 A: `memory_recall_clean`
- 首轮读取事实
- 次轮直接提问
- 指标：正确召回率、重复读取次数

### 实验 B: `memory_recall_with_noise`
- 首轮写入目标事实
- 插入多条无关记忆
- 次轮提问
- 指标：正确召回率、无关召回率

### 实验 C: `memory_conflict_resolution`
- 先给旧事实，再给新事实
- 次轮要求按最新事实执行
- 指标：stale recall rate

### 实验 D: `memory_cross_session`
- session 1 写入事实
- session 2 恢复并执行
- 指标：跨轮可用率、噪声率

## 6.3 推荐指标

| 指标 | 含义 |
|------|------|
| `correct_recall_rate` | 正确召回比例 |
| `irrelevant_recall_rate` | 召回无关记忆比例 |
| `stale_recall_rate` | 错误使用旧知识比例 |
| `repeated_reads` | 因记忆失败导致的重复读文件次数 |
| `semantic_record_count` | 长期记忆条目数 |
| `semantic_noise_ratio` | 噪声条目占比 |

---

## 7. Trace 专项验证设计

`trace` 可解释性建议单独定义成结构校验，而不是只靠人工读样例。

## 7.1 必备事件

每个 run 至少应包含：

- `run_started`
- `state_transition`
- `context_built`
- `prompt_built`
- `model_requested`
- `model_parsed`
- `tool_executed` 或明确的失败事件
- `run_finished`

## 7.2 可解释性要求

trace 至少要能回答：

1. 当前运行在哪个阶段结束
2. prompt 中有哪些 section，被裁掉了什么
3. 为什么发起这次工具调用
4. 为什么停止
5. 记忆在什么时候写入和召回

## 7.3 建议指标

| 指标 | 含义 |
|------|------|
| `trace_completeness_rate` | 必备事件齐全比例 |
| `trace_order_valid_rate` | 事件顺序合法比例 |
| `context_built_coverage` | 有上下文构建记录的 run 比例 |
| `stop_reason_trace_match_rate` | trace 结束事件与 report 停止原因一致比例 |

---

## 8. 建议扩展 Benchmark Schema

为了支持失败任务和行为型评测，建议在 task schema 中新增字段：

```json
{
  "id": "stop_reason_step_limit",
  "prompt": "Update the config using the remembered field name.",
  "fixture_repo": "tests/fixtures/bench_v2_failure_modes",
  "allowed_tools": ["read_file", "patch_file"],
  "step_budget": 1,
  "expected_artifact": "task should stop before completion",
  "verifier": "python -c \"...\"",
  "category": "failure",
  "expected_status": "stopped",
  "expected_stop_reason": "step_limit_reached",
  "expected_failure_category": "failure_stop_reason",
  "tags": ["stop_reason", "failure-mode"]
}
```

推荐新增字段如下：

- `expected_status`
- `expected_stop_reason`
- `expected_failure_category`
- `tags`
- `session_setup`
- `trace_expectations`
- `metrics_assertions`

这样 evaluator 才能支持“预期失败也是通过”的任务。

---

## 9. 验收指标

以下指标建议作为第一版硬门槛：

| 目标 | 指标 | 建议门槛 |
|------|------|----------|
| Prompt 稳定性 | 同任务 `prompt_chars` 变异系数 | `<= 12%` |
| 无关上下文减少 | `irrelevant_recall_rate` | `< 10%` |
| 重复工具调用下降 | `repeated_identical_call_count` 平均值 | `<= 0.2` |
| Stop reason 清晰 | `expected_stop_reason` 命中率 | `>= 95%` |
| 长期记忆噪声更少 | `stale_recall_rate` | `< 5%` |
| Trace 更可解释 | `trace_completeness_rate` | `100%` |

如果第一版数据不稳定，可以先把这些门槛用于“趋势判断”，等样本数扩大后再转成硬限制。

---

## 10. 落地实施顺序

建议分 3 个迭代完成。

### Iteration 1: 最小可用版本

目标：

- 保留现有 `coding_tasks.json`
- 新增 `refactor_behavior_v1.json`
- 新增 8 个任务
- 支持行为型指标汇总

实施项：

- 新建 `benchmarks/refactor_behavior_v1.json`
- 新建 3 组 fixture
- 扩展 evaluator 的 task schema
- 在 campaign 报告中新增行为指标小节

### Iteration 2: 失败与停止原因

目标：

- 新增 `refactor_failure_v1.json`
- 支持“预期失败也算 pass”
- 输出 stop reason 命中率

实施项：

- 扩展 `OutcomeEvaluator`
- 扩展 `FailureAnalyzer`
- 为失败任务新增 verifier 口径

### Iteration 3: 长期记忆与 trace 质量

目标：

- 把记忆噪声和 trace 完整性纳入统一报告
- 建立最终可对外使用的 refactor evaluation 报告模板

实施项：

- 扩展 `collect_resume_metrics()`
- 增加 trace 完整性统计
- 增加跨 session 记忆实验

---

## 11. 推荐命令接口

最终建议保留两个入口：

### 1. 回归入口

```powershell
python scripts/run_eval_campaign.py --benchmark-path benchmarks/coding_tasks.json --iterations 20 --mode full
```

### 2. 重构评测入口

```powershell
python scripts/run_refactor_eval.py --behavior benchmarks/refactor_behavior_v1.json --failure benchmarks/refactor_failure_v1.json --iterations 20 --mode full
```

重构评测报告建议输出：

- `behavior_summary.json`
- `failure_summary.json`
- `memory_report.json`
- `trace_report.json`
- `refactor_eval_report_zh.md`

---

## 12. 最终建议

最重要的不是“做一个更大的 benchmark”，而是把评测职责拆清楚：

- 现有 benchmark 负责证明“系统没坏”
- 新的行为型 benchmark 负责证明“行为变好了”
- 新的 diagnostics experiments 负责证明“上下文和记忆机制真的有效”

如果只扩充任务数量，而不扩充 schema、失败口径和专项指标，那么新的 benchmark 仍然无法回答你最关心的 6 个问题。

因此推荐的落地路线是：

1. 保留现有回归集
2. 新增行为型 benchmark
3. 新增失败型 benchmark
4. 保留并扩展 context/memory/trace 专项实验
5. 最终统一输出一份中文重构评测报告
