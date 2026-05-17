# Project Update Discussion Plan

> **Review Summary (2026-05-17):** Phase 0-1 已完成。Phase 2-4 核心模块已实现，缺少验证测试。Phase 5-6 尚未开始。文档已从 "draft" 更新为 "needs update" 并标注各阶段当前状态。

> **Clean Review Summary (2026-05-17):** Phase 0 baseline checks, Phase 1 documentation repair, Phase 2 memory pipeline hardening, Phase 3 recall/context integration, Phase 4 benchmark regression coverage, Phase 5 pluggable skill/memory service, and Phase 6 lightweight multi-agent state model are complete.
> Status: in progress  
> Scope: `owl` package and repository-level memory/runtime update work  
> Date: 2026-05-17
> Last Reviewed: 2026-05-17

## 0. Implementation Progress

### 2026-05-17: Phase 0 / Phase 1 Initial Pass

Completed:

- Established a baseline with targeted tests for benchmark, memory, validity, and context builder modules.
- Repaired garbled docstrings and comments in:
  - `owl/benchmark_runner.py`
  - `owl/benchmark_model_clients.py`
  - `owl/memory_compactor.py`
- Kept runtime behavior unchanged; the edits are documentation/readability focused.
- Ran targeted tests:

```bash
python -m pytest tests\test_benchmark_runner.py tests\test_memory_new_modules.py tests\test_memory_validity.py tests\test_context_builder.py
```

Result:

```text
85 passed
```

- Ran lint for the edited files:

```bash
uv run ruff check owl\benchmark_runner.py owl\benchmark_model_clients.py owl\memory_compactor.py
```

Result:

```text
All checks passed
```

- Ran the full test suite:

```bash
python -m pytest
```

Result:

```text
369 passed, 1 skipped
```

The skipped test is the existing Windows symlink privilege skip in `tests\test_safety_invariants.py`.

### 2026-05-17: Phase 2 Memory Pipeline Hardening

Completed:

- Audited runtime memory writes and verified tool execution does not call `MemoryWriter.write_semantic()`.
- Kept tool-result writes on the working-memory path:

```text
tool result -> MemoryWriter.should_write() -> MemoryWriter.write_working()
```

- Added `MemoryCompactor.apply_write_intents()` so semantic invalidation for modified files is handled by the compactor, not directly by runtime or `MemoryWriter`.
- Removed runtime's private direct semantic invalidation helper.
- Updated file modification events so they are recorded in `WorkingMemory` with file path and fingerprint metadata.
- Prevented compaction from promoting stale read summaries for files modified in the same run.
- Added tests for:
  - file modification intent written to working memory
  - compactor-driven semantic invalidation
  - modified files not being promoted from stale read summaries

Verification:

```bash
python -m pytest tests\test_memory_new_modules.py tests\test_memory_validity.py tests\test_owl.py
```

Result:

```text
99 passed
```

```bash
uv run ruff check owl\memory_writer.py owl\memory_compactor.py owl\runtime.py tests\test_memory_new_modules.py
```

Result:

```text
All checks passed
```

```bash
python -m pytest
```

Result:

```text
372 passed, 1 skipped
```

Full repository lint note:

```bash
uv run ruff check .
```

Result:

```text
Failed with existing repository-wide lint issues outside this Phase 2 change set.
The edited Phase 2 files pass targeted ruff checks.
```

### 2026-05-17: Phase 3 Recall And Context Integration

Completed:

- Updated `ContextManager` so new `MemoryRetriever` recall and legacy episodic recall are not silently mixed.
- New recall results are preferred when semantic or non-trivial retrieved memory exists.
- Legacy episodic recall is used only as fallback when new recall is empty, or when new recall is only a working-memory echo.
- Added prompt metadata fields:
  - `recall_source`
  - `new_recall_used`
  - `legacy_recall_used`
- Added tests proving:
  - new semantic recall suppresses legacy note injection
  - empty new recall falls back to legacy notes
  - working-only recall still allows legacy fallback

Verification:

```bash
python -m pytest tests\test_context_manager.py tests\test_context_builder.py tests\test_owl.py
```

Result:

```text
68 passed
```

```bash
uv run ruff check owl\context_manager.py tests\test_context_manager.py
```

Result:

```text
All checks passed
```

```bash
python -m pytest
```

Result:

```text
375 passed, 1 skipped
```

### 2026-05-17: Phase 4 Benchmark And Regression Coverage

Completed:

- Added comparison coverage for artifacts where baseline and candidate contain different task IDs.
- Preserved and verified per-category delta coverage.
- Added evaluator regression tests proving failure-mode benchmark tasks use the custom model clients:
  - `RetryTriggeringModelClient` -> `retry_limit_reached`
  - `ErrorInjectingModelClient` -> `model_error`
- Removed unused imports from benchmark tests.

Verification:

```bash
python -m pytest tests\test_benchmark_runner.py tests\test_evaluator.py
```

Result:

```text
17 passed
```

```bash
uv run ruff check owl\benchmark_runner.py owl\benchmark_model_clients.py tests\test_benchmark_runner.py tests\test_evaluator.py
```

Result:

```text
All checks passed
```

```bash
python -m pytest
```

Result:

```text
378 passed, 1 skipped
```

### 2026-05-17: Phase 5 Pluggable Skill And Memory Service

Completed:

- Added `owl/skill_memory_service.py` as a read-only external-facing facade.
- Implemented compact skill recall context packets through `SkillMemoryService.recall_skill()`.
- Added sanitized export through `SkillMemoryService.export_skills()`.
- Kept external observation writes read-only by default; `write_observation()` rejects mutation in this first service phase.
- Added a placeholder `compact_run()` response so future compaction wiring has a stable API location.
- Added the `owl-skill` CLI entry point with `recall` and `export` commands.
- Added lazy package export for `SkillMemoryService` so `python -m owl.skill_memory_service --help` runs without a runpy warning.
- Added tests covering skill matching, packet formatting, sanitized export, read-only write rejection, output limits, and package export.

Verification:

```bash
python -m pytest tests\test_skill_memory_service.py tests\test_procedure_candidates.py tests\test_owl.py::test_public_api_exports_resolve_through_package_path
```

Result:

```text
28 passed
```

```bash
uv run ruff check owl\skill_memory_service.py owl\__init__.py tests\test_skill_memory_service.py
```

Result:

```text
All checks passed
```

```bash
python -m owl.skill_memory_service --help
```

Result:

```text
CLI help renders successfully.
```

```bash
python -m pytest
```

Result:

```text
383 passed, 1 skipped
```

The skipped test is the existing Windows symlink privilege skip in `tests\test_safety_invariants.py`.

### 2026-05-17: Phase 6 Lightweight Multi-Agent State Model

Completed:

- Added `owl/task_graph_state.py` as a small fixed-role request lifecycle state model.
- Kept `Owl.ask()` as the executor; no graph framework or dynamic agent swarm was introduced.
- Mapped existing runtime stages into explicit state fields:
  - context findings
  - selected files
  - relevant memory
  - execution plan
  - tool observations
  - modified files
  - verification results
  - final answer
  - memory compaction report
- Added `task_graph_state` to runtime reports.
- Added lazy package export for `TaskGraphState`.
- Added tests for state initialization, updates, serialization, stopped completion, and a real `Owl.ask()` tool-write path.

Verification:

```bash
python -m pytest tests\test_task_graph_state.py tests\test_owl.py::test_agent_populates_task_graph_state tests\test_execution_state.py
```

Result:

```text
19 passed
```

```bash
uv run ruff check owl\task_graph_state.py owl\runtime.py owl\__init__.py tests\test_task_graph_state.py tests\test_owl.py
```

Result:

```text
All checks passed
```

```bash
python -m pytest
```

Result:

```text
387 passed, 1 skipped
```

The skipped test is the existing Windows symlink privilege skip in `tests\test_safety_invariants.py`.

Repository-wide lint note:

```bash
uv run ruff check .
```

Result:

```text
Failed with existing repository-wide lint issues outside the Phase 6 change set.
The edited Phase 6 files pass targeted ruff checks.
```

## 1. Current Understanding

The project is a local coding agent with a layered memory design. The intended architecture is:

`WorkingMemory -> MemoryCompactor -> SemanticMemory`

Context is assembled by `ContextBuilder` from working memory, semantic memory, and context discovery. Repository rules also require:

- All memory writes go through `MemoryWriter`.
- All memory recalls go through `MemoryRetriever`.
- New behavior should have tests.
- Existing module structure and patterns should be preserved.

The active files suggest the next update is likely centered on the CLI/runtime entry path, benchmark utilities, and the memory compaction pipeline:

- `owl/__main__.py`
- `owl/__init__.py`
- `owl/benchmark_model_clients.py`
- `owl/benchmark_runner.py`
- `owl/memory_compactor.py`

## 2. Observed Issues To Discuss

### 2.1 Documentation Encoding / Mojibake

> **Status: DONE** (commit `50b6a27` — Refactor memory pipeline and sanitize repo assets)
>
> All key Python files are now well-formed UTF-8. Chinese docstrings render correctly.
> No further action needed unless new encoding regressions appear.

### 2.2 Memory Pipeline Consistency

The memory pipeline is now implemented with the intended architecture:

```
tool result -> MemoryWriter -> WorkingMemory -> MemoryCompactor -> SemanticMemory
```

Implemented modules:
- `MemoryWriter`: unified write policy with `should_write()`, `write_working()`, `write_semantic()`
- `MemoryCompactor`: `compact_and_promote()` with schema-based compaction
- `MemoryRetriever`: multi-dimensional recall with `RecallRanker`
- `StaleObservationGuard`: file fingerprint tracking for stale detection
- `SemanticRecordValidityChecker`: record invalidation

Remaining work:

- Verify `compact_and_promote_v2()` is the sole semantic promotion path at runtime.
- Confirm no direct semantic writes remain in the tool loop outside `MemoryWriter`.
- Mark legacy `LayeredMemory` as compatibility-only with a deprecation note.

### 2.3 Context Assembly Source Of Truth

> **Status: IMPLEMENTED** — `ContextManager` provides the 5-layer context model

`ContextManager` already owns prompt assembly with defined layers:

- `RESIDENT`: persistent repo context (AGENTS.md, README.md)
- `ON_DEMAND`: discovered context
- `RUNTIME`: working memory and recent observations
- `COMPACTED`: promoted semantic records
- `SYSTEM`: system prompts and instructions

`MemoryRetriever` is the sole recall path for `relevant_memory` in prompts.

Acceptance criteria verification needed:

- Prompt metadata shows which notes were selected.
- Recall behavior is deterministic in tests.
- Legacy and new memory sources are not mixed silently in the same section.

### 2.4 Benchmark Utilities

`benchmark_runner.py` and `benchmark_model_clients.py` are implemented with clean docstrings.

Current API:
- `BenchmarkResult`: loads and queries a single artifact
- `ComparisonReport`: baseline vs candidate comparison
- `BenchmarkRunner`: batch runner calling `evaluator.run_fixed_benchmark`
- `RetryTriggeringModelClient`, `ErrorInjectingModelClient`: failure-mode testing clients

Remaining work:

- Add tests for missing task IDs across baseline/candidate artifacts.
- Add tests for category-level pass-rate deltas.
- Verify test file path: `tests/test_benchmark_runner.py` (located at project root `e:\agentcode\owl\tests/`).

### 2.5 Test And Quality Gate

The repo standard requires:

```bash
ruff check .
pytest
```

For memory/runtime changes, targeted tests are at `e:\agentcode\owl\tests/`:

```bash
pytest tests/test_memory_new_modules.py tests/test_memory_validity.py tests/test_context_builder.py
pytest tests/test_benchmark_runner.py
```

Then run the full suite before committing.

## 3. Multi-Agent Architecture Evaluation And Recommendation

This section is based on the review of `E:\agent\wiki\sources\tradingagents-github.md`.

### 3.1 What To Borrow From TradingAgents

TradingAgents is useful as an architecture reference because it is not an open-ended agent swarm. It uses a fixed, graph-orchestrated workflow:

`analysts -> bull/bear debate -> research manager -> trader -> risk debate -> portfolio manager`

The reusable design ideas are:

- Fixed topology instead of dynamic agent spawning.
- Narrow role ownership for each agent.
- Explicit shared state fields instead of relying only on chat history.
- Bounded debate / review loops.
- Manager or reducer nodes that turn multiple opinions into one decision artifact.
- A final authority node that owns the final decision.

For `owl`, the most important takeaway is:

Multi-agent should mean structured role separation, not uncontrolled parallel conversations.

### 3.2 Fit For Owl

`owl` already has several components that map naturally to a lightweight multi-agent design:

- `runtime.py`: main controller and executor.
- `planner.py`: planning role.
- `verifier.py`: verification role.
- `ContextDiscovery` / `ContextManager`: context analysis role.
- `MemoryWriter`, `MemoryRetriever`, `MemoryCompactor`: shared memory and memory-curation roles.
- `delegate`: limited subtask delegation.

Because these pieces already exist, the project should not be rewritten around a large agent framework. The better path is to formalize the current runtime into a small fixed workflow.

Recommended shape:

`User Request -> Context Analyst -> Planner -> Executor -> Verifier/Reviewer -> Memory Curator -> Final Responder`

The `Executor` should remain the existing `Owl.ask()` tool loop. It does not need to become a separate autonomous agent.

### 3.3 Proposed Lightweight Role Model

#### Context Analyst

Responsibility:

- Discover relevant files and context.
- Recall relevant memory through `MemoryRetriever`.
- Produce structured context artifacts.

State outputs:

- `context_findings`
- `selected_files`
- `relevant_memory`

#### Planner

Responsibility:

- Convert the user request and context findings into a concise execution plan.
- Avoid over-planning trivial requests.

State outputs:

- `execution_plan`
- `plan_rationale`

#### Executor

Responsibility:

- Run the existing tool loop.
- Write observations to `WorkingMemory` through `MemoryWriter`.
- Preserve approval and safety behavior.

State outputs:

- `tool_observations`
- `modified_files`
- `execution_errors`

#### Verifier / Reviewer

Responsibility:

- Check whether the execution result satisfies the request.
- Identify missing tests, risky changes, or incomplete work.
- Keep verification bounded.

State outputs:

- `verification_results`
- `review_findings`
- `completion_status`

#### Memory Curator

Responsibility:

- Compact working memory.
- Promote durable knowledge to semantic memory.
- Keep long-term memory writes centralized.

State outputs:

- `memory_compaction_report`
- `promoted_records`
- `skipped_records`

### 3.4 Shared State Recommendation

Introduce a small explicit state object rather than passing unstructured role transcripts.

Possible state fields:

```text
TaskGraphState:
- user_request
- context_findings
- selected_files
- relevant_memory
- execution_plan
- tool_observations
- modified_files
- verification_results
- final_answer
- memory_compaction_report
```

This state should be treated as an internal runtime artifact. It should not replace `WorkingMemory` or `SemanticMemory`; it should coordinate one request lifecycle.

### 3.5 What Not To Adopt

Do not adopt these patterns in the first implementation:

- Free-form multi-agent group chat.
- Dynamic agent spawning as the default execution model.
- Long debate loops.
- One agent per tool.
- Multiple agents writing directly to `SemanticMemory`.
- A full LangGraph dependency before the lightweight state model proves useful.

These choices would increase complexity before the project has a verified need for them.

### 3.6 Recommendation

Adopt a lightweight, fixed-role multi-agent architecture.

The implementation should be incremental:

1. Add explicit request lifecycle state.
2. Map existing context/planner/executor/verifier/memory behavior into that state.
3. Add tests for state transitions and role outputs.
4. Only consider a graph framework later if the fixed workflow becomes too hard to maintain manually.

The goal is to get TradingAgents' strengths: auditability, role clarity, bounded loops, and structured outputs, without importing its domain-specific complexity.

## 4. Pluggable Skill And Memory Sub-Agent Plan

### 4.1 Final Product Goal

The long-term goal is to make `owl` useful across two different environments:

```text
Closed development environment:
local LLM + owl
  -> diagnose and repair code
  -> record errors, context, fixes, and verification commands
  -> distill repeated successful patterns into skills / workflows

Open development environment:
external coding agent
  -> calls owl as a tool or sub-agent
  -> retrieves relevant skills learned in the closed environment
  -> injects a compact workflow packet into the main agent context
  -> solves similar errors faster with fewer tokens
```

This means `owl` should evolve into a transferable engineering experience layer, not only a standalone coding agent.

### 4.2 Core Concept

The system should separate three kinds of knowledge:

- `Memory`: project-specific facts, observations, files, run history, and semantic records.
- `Skill`: generalized workflow extracted from repeated or successful repair patterns.
- `Context Packet`: short, structured output returned to another agent for one request.

Closed-environment runs can use detailed local memory. Open-environment calls should prefer sanitized and generalized skills instead of raw project memory.

### 4.3 Target Architecture

```text
Closed Environment
  -> Owl Runtime
  -> WorkingMemory
  -> MemoryCompactor
  -> SemanticMemory
  -> Skill Candidate Detector
  -> Skill Registry / Workflow Store

Open Environment
  -> External Code Agent
  -> owl_skill_recall(query, repo_profile, error_signature)
  -> Owl Skill Service
  -> Context Packet
  -> Main agent prompt/context injection
```

`owl` should not directly take over the external agent's task loop. It should provide retrieved workflows, warnings, and verification suggestions as a tool result.

### 4.4 Skill Shape

A useful workflow skill should be structured around engineering behavior, not around a chat transcript.

Recommended fields:

```text
Skill:
- skill_id
- title
- trigger_conditions
- diagnosis_steps
- repair_steps
- verification_steps
- anti_patterns
- applicable_repo_profiles
- applicable_repo_paths
- required_tools
- confidence
- usage_count
- success_count
- source_run_ids
- sanitized
```

Example context packet returned to an external agent:

```json
{
  "matched_workflow": "pytest-import-error-local-package",
  "confidence": 0.86,
  "when_to_use": [
    "pytest fails with ModuleNotFoundError",
    "package imports work from repo root but fail in tests"
  ],
  "steps": [
    "Check pyproject package config",
    "Check tests/conftest.py path setup",
    "Run targeted pytest before full suite"
  ],
  "verification": [
    "pytest tests/test_imports.py",
    "ruff check ."
  ],
  "cautions": [
    "Do not rewrite package layout before checking editable install"
  ],
  "prompt_injection": "Relevant workflow: ..."
}
```

### 4.5 Isolation And Non-Pollution Requirements

The external agent integration must not pollute the external project context or leak closed-environment details.

Requirements:

- Every memory store must be namespace-scoped by repo identity or explicit workspace profile.
- External calls must have `max_tokens` or equivalent output limits.
- Raw memory should not be returned by default.
- Exported skills must be sanitized before use outside the closed environment.
- Secrets, internal hostnames, customer names, private absolute paths, and private API identifiers must be redacted or generalized.
- The default external call should be read-only.
- Long-term writes must still go through `MemoryWriter` and compaction.
- Recalls must still go through `MemoryRetriever` or a skill-specific retriever built on the same principles.

### 4.6 API Direction

The first interface should be a local Python service layer and CLI. MCP can come later after the behavior is stable.

Suggested service API:

```python
class SkillMemoryService:
    def recall_skill(repo_profile, query, error_signature=None, max_tokens=1200):
        ...

    def write_observation(repo_root, event):
        ...

    def compact_run(repo_root, run_id):
        ...

    def export_skills(repo_root, destination, sanitize=True):
        ...
```

Suggested CLI:

```bash
owl-skill recall --query "pytest import error" --max-tokens 1200
owl-skill export --repo E:\closed-project --out E:\shared-skills --sanitize
owl-memory compact --repo E:\closed-project --run-id <run_id>
```

Suggested later MCP tools:

- `owl_skill_recall`
- `owl_context_pack`
- `owl_memory_compact`
- `owl_skill_export`

### 4.7 Main Risks

Private information leakage:

Closed-environment skills must be sanitized and generalized before export.

Wrong transfer:

A workflow that worked in one project may be harmful in another. Skills need applicability conditions and anti-patterns.

Over-injection:

External agents should receive a compact context packet, not an entire memory dump.

Unstable memory boundaries:

The memory pipeline must be hardened first. Otherwise external callers will amplify inconsistent write and recall behavior.

Role confusion:

The external-facing `owl` service should provide memory, workflow recall, and context packets. It should not modify external code unless a later explicit mode is designed.

### 4.8 Recommendation

This direction is feasible and should become a major product goal:

`owl` should become a local skill/memory sub-agent that learns engineering workflows in closed environments and exposes transferable, sanitized workflow packets to other coding agents through tool use.

Implementation order:

1. Harden memory writes, compaction, and recall.
2. Stabilize skill candidate detection and skill registry schema.
3. Add a local `SkillMemoryService`.
4. Add CLI access for recall/export/compact.
5. Add sanitization and export rules.
6. Add MCP integration after the CLI/service behavior is proven.
7. Measure token reduction, speed, and success rate on repeated benchmark tasks.

## 5. Proposed Update Phases

### Phase 0: Baseline And Safety

> **Status: DONE**

The baseline is established. Memory pipeline, context system, and benchmark modules are in place.
Targeted tests exist at `e:\agentcode\owl\tests/`.

### Phase 1: Documentation Repair

> **Status: DONE** (encoding fixed in commit `50b6a27`)

High-traffic modules are documented with UTF-8 Chinese docstrings. The `MEMORY_REFACTOR_IMPLEMENTATION_PLAN.md` at project root provides detailed memory architecture documentation.

### Phase 2: Memory Pipeline Hardening

> **Status: PARTIAL** — Core pipeline implemented, verification tests needed

> **Current Status: DONE** (verified on 2026-05-17)

Memory pipeline is implemented:
```
tool result -> MemoryWriter -> WorkingMemory -> MemoryCompactor -> SemanticMemory
```

Completed verification tasks:

- [x] Audit runtime memory writes - verify no direct semantic writes during tool execution
- [x] Add tests proving a single tool read writes working memory but does not immediately write semantic memory
- [x] Add tests proving finalization/compaction promotes eligible records
- [x] Add tests proving file modifications invalidate stale summaries

Remaining follow-up:

- [ ] Mark legacy `LayeredMemory` as compatibility-only with a deprecation note

Verification:

- Targeted memory tests: `pytest tests/test_memory_new_modules.py tests/test_memory_validity.py`
- Full memory suite: `pytest tests/test_memory.py tests/test_memory_new_modules.py tests/test_memory_validity.py tests/test_recall_ranker.py`

### Phase 3: Recall And Context Integration

> **Status: PARTIAL** — Modules implemented, tests needed

`ContextManager` with 5-layer context model is implemented. `MemoryRetriever` is the sole recall path.

> **Current Status: DONE** (verified on 2026-05-17)

Completed verification tasks:

- [x] Ensure prompt metadata shows which notes were selected and which recall source was used
- [x] Add tests for new recall vs legacy fallback behavior
- [x] Verify prompt sections stay within configured budgets
- [x] Ensure no silent mixing of legacy `LayeredMemory` retrieval and semantic recall

Verification:

- `pytest tests/test_context_builder.py tests/test_context_manager.py tests/test_context_layers.py`

### Phase 4: Benchmark And Regression Coverage

> **Status: PARTIAL** — Benchmark modules implemented, edge-case tests needed

> **Current Status: DONE** (verified on 2026-05-17)

Completed tasks:

- [x] Add tests for missing task IDs across baseline/candidate artifacts
- [x] Add tests for category-level pass-rate deltas
- [x] Use failure-mode model clients (`RetryTriggeringModelClient`, `ErrorInjectingModelClient`) in stop-reason tests

Verification:

- `pytest tests/test_benchmark_runner.py tests/test_evaluator.py`

### Phase 5: Pluggable Skill And Memory Service

> **Current Status: DONE** (verified on 2026-05-17)

Goal:

Make `owl` callable by other coding agents as a read-only memory/skill tool.

Tasks:

- [x] Define the skill/workflow schema.
- [x] Stabilize `SkillCandidateRegistry` as the workflow store.
- [x] Add a `SkillMemoryService` facade for skill recall, compaction placeholder, and export.
- [x] Add a CLI wrapper for recall/export.
- [x] Add sanitization rules for exported workflows.
- [x] Return compact context packets rather than raw memory dumps.

Verification:

- [x] Tests cover skill matching and context packet formatting.
- [x] Tests cover sanitized export.
- [x] Tests prove external recall does not write to project memory by default.
- [x] Tests cover token/output limits.

### Phase 6: Lightweight Multi-Agent State Model

> **Current Status: DONE** (verified on 2026-05-17)

Goal:

Formalize the current runtime into a fixed-role lifecycle without introducing a heavy graph framework.

Tasks:

- [x] Add a small `TaskGraphState` internal state model.
- [x] Map context findings, execution plan, tool observations, verification results, and memory compaction reports into explicit fields.
- [x] Keep `Owl.ask()` as the main executor.
- [x] Keep all memory writes and recalls routed through the existing memory modules.
- [x] Add report metadata that shows role-stage transitions.

Verification:

- [x] Tests cover state initialization and updates.
- [x] Runtime tests prove tool execution still uses the existing `Owl.ask()` memory path.
- [x] Verification state is recorded without adding unbounded reviewer loops.

## 6. Suggested Next Concrete Change Set

Phase 0-6 are now implemented. The next change set should be a stabilization and documentation pass before adding a larger MCP integration:

1. Add or update user-facing docs for `owl-skill recall` and `owl-skill export`.
2. Add a compatibility/deprecation note for legacy `LayeredMemory`.
3. Decide whether `compact_run()` should remain a placeholder or become an active CLI/API path.
4. Run the full test suite again after the documentation/API cleanup.
5. Commit the completed Phase 2-6 implementation as a checkpoint.

Reason:

The core architecture is now in place. The next risk is not missing code, but unclear external usage boundaries for other coding agents.

## 7. Open Questions For You

1. Should the existing `MEMORY_REFACTOR_IMPLEMENTATION_PLAN.md` remain the main memory refactor source of truth?
2. Do you want the current Phase 2-6 work committed as one checkpoint or split by phase?
3. Should MCP be treated as a first-class target now, or only after the CLI service is used manually for a while?
4. Should exported skills be English-only, Chinese-only, or language-preserving based on source runs?
5. Should `compact_run()` be implemented as an active external command in the next phase?

## 8. Definition Of Done

The update work should be considered complete when:

- The agreed phase scope is implemented.
- New or changed behavior has tests.
- `ruff check .` passes.
- Relevant targeted tests pass.
- Full `pytest` is run, or any inability to run it is documented.
- Git status is clean or remaining changes are intentionally listed.
