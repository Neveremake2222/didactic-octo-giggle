# Memory Refactor Implementation Plan

> Scope: align runtime behavior with the target design described in `MEMORY_ARCHITECTURE.md`
> Status: planning document
> Priority order: P0 -> P1 -> P2 -> P3 -> P4 -> P5

---

## 1. Goals

This refactor is meant to close the gap between the intended memory architecture and the current runtime behavior.

Primary goals:

1. Re-establish a single memory pipeline:
   `tool result -> WorkingMemory -> MemoryCompactor -> SemanticMemory`
2. Make `SemanticMemory` truly persistent across runs.
3. Make staleness / validity checks effective in runtime, not only in tests.
4. Remove mixed-source prompt assembly from legacy and new memory systems.
5. Improve recall consistency and maintainability after the core pipeline is stable.

Non-goals for the first wave:

- Replacing the CLI UX
- Adding embeddings / vector DB
- Rewriting benchmark logic
- Removing all legacy code in one step

---

## 2. Current Gaps

The current codebase has five important architecture gaps:

1. `SemanticMemory` is instantiated without a database path in runtime, so long-term memory is not actually persistent across runs.
2. Runtime directly calls `MemoryWriter.write_semantic()` after each tool execution, which breaks the "MemoryCompactor is the only bridge" rule.
3. `FileFingerprintTracker` and `StaleObservationGuard` exist, but the tracker is not populated from the real runtime tool flow, so staleness handling is mostly inert.
4. Prompt assembly still mixes legacy `LayeredMemory` with the new memory stack.
5. `SemanticRecord.file_path` exists in the data model and invalidation API, but most writes only populate `repo_path`, so invalidation and file-scoped search are incomplete.

---

## 3. Refactor Strategy

Implement in six phases:

### P0. Core Pipeline Consistency

Objective:
Only one path is allowed to write long-term memory.

Required outcome:

- Tool execution writes into working memory only.
- Semantic promotion happens only at compaction/finalization.
- Legacy memory stops being a second writable source of truth.

### P1. Persistent Semantic Memory

Objective:
Turn semantic memory into a real repo-scoped persistent store.

Required outcome:

- `SemanticMemory` uses `.owl/memory/semantic-memory.db`
- A new process can reuse previously promoted semantic records.

### P2. Runtime Validity and Invalidation

Objective:
Make file change detection actually drive stale observation cleanup and semantic invalidation.

Required outcome:

- `read_file` records fingerprints
- `write_file/patch_file` updates fingerprints and invalidates old semantic summaries
- recall filters out file-backed stale records

### P3. Prompt and Recall Source Unification

Objective:
Use the new memory stack as the primary prompt source.

Required outcome:

- `memory` section uses new working memory view
- `relevant_memory` comes from `MemoryRetriever`
- legacy recall is no longer mixed in the same prompt path

### P4. Retrieval Quality Improvements

Objective:
Improve recall quality after behavior is stable.

Required outcome:

- Better tokenization for code/path text
- SQLite search behavior aligned with in-memory search
- More consistent ranker behavior

### P5. Legacy Layer Cleanup

Objective:
Reduce maintenance burden after new stack is stable.

Required outcome:

- legacy memory becomes compatibility-only or removable
- tests migrate to the new source of truth

---

## 4. File-by-File Implementation Checklist

## P0. Core Pipeline Consistency

### `owl/runtime.py`

Tasks:

- Stop calling `self._memory_writer.write_semantic(...)` directly in the tool loop.
- Keep only `should_write(...)` + `write_working(...)` in the tool execution path.
- Keep semantic promotion in `_finalize_success()` and `_finalize_stop()` via `MemoryCompactor`.
- Decide whether `update_memory_after_tool()` remains as temporary legacy compatibility or is disabled behind a feature flag.
- Add trace fields that explicitly distinguish:
  - `memory_written_working`
  - `memory_promoted_semantic`

Acceptance:

- No semantic write occurs immediately after a single tool call.
- Semantic writes happen only in finalization/compaction.

### `owl/memory_writer.py`

Tasks:

- Split responsibilities clearly:
  - `should_write()` decides whether a tool result is worth remembering
  - `write_working()` writes to working memory
  - semantic write path becomes either:
    - removed from runtime use, or
    - renamed to a compaction-only helper
- Introduce an explicit invalidation intent for file modifications, instead of directly deleting semantic records here.
- Return richer decisions, e.g.:
  - `promote_candidate`
  - `invalidate_paths`
  - `importance_hint`

Acceptance:

- Writer no longer acts as a second semantic promotion channel during tool execution.

### `owl/memory_compactor.py`

Tasks:

- Make `compact_and_promote_v2()` the only semantic promotion path.
- Consume write intents / working observations to:
  - invalidate outdated file summaries
  - promote multi-observation file summaries
  - write structured run summaries
- Ensure promoted records always include:
  - `repo_path`
  - `file_path` when file-backed
  - `file_version`
  - `freshness_hash`
  - `importance_score`
- Revisit `promote_to_semantic()` so it uses structured fields when available, not only summary string parsing.

Acceptance:

- Compactor becomes the only bridge from working memory to semantic memory.

### `owl/memory.py`

Tasks:

- Mark legacy layer as compatibility-only in comments and usage.
- Identify every runtime write path still targeting legacy memory.
- Prepare for staged shutdown of:
  - file summaries
  - episodic note writes
  - retrieval candidates in prompt assembly

Acceptance:

- Legacy memory is no longer silently competing with the new stack.

---

## P1. Persistent Semantic Memory

### `owl/runtime.py`

Tasks:

- Instantiate semantic memory with a repo-scoped db path:
  - `.owl/memory/semantic-memory.db`
- Ensure `from_session()` rebuilds the same semantic store from repo root rather than creating a fresh in-memory object.
- Ensure shutdown/finalization closes the DB cleanly if needed.

Acceptance:

- Restarting the process preserves semantic records for the same repo.

### `owl/semantic_memory.py`

Tasks:

- Confirm DB initialization path logic is correct for repo-scoped storage.
- Add small helper for default repo DB path if useful.
- Verify consistency between in-memory fallback and SQLite mode.
- Ensure `count()`, `all_records()`, and `search()` behave the same in both modes.

Acceptance:

- SQLite mode is the default runtime mode for repo execution.
- In-memory mode remains only as fallback, not normal operation.

### `owl/run_store.py`

Tasks:

- Optionally record semantic DB metadata in reports or metrics:
  - db path
  - active record count
  - total record count

Acceptance:

- Run artifacts expose enough information to debug semantic persistence.

---

## P2. Runtime Validity and Invalidation

### `owl/runtime.py`

Tasks:

- After successful `read_file`, record fingerprint into the shared tracker.
- After successful `write_file` / `patch_file`:
  - update tracker
  - invalidate semantic records for the affected file
- Ensure invalidation is applied before later recall in the same run.
- Emit trace events for:
  - fingerprint recorded
  - semantic invalidated by file
  - stale observations removed

Acceptance:

- File changes in the same run affect both working and semantic memory behavior immediately.

### `owl/memory_validity.py`

Tasks:

- Keep `FileFingerprintTracker` as the single tracker implementation.
- Extend `SemanticRecordValidityChecker` usage contract so file-backed semantic records can be checked in recall flow.
- Clarify whether missing files mean:
  - stale
  - invalidated
  - unknown

Acceptance:

- Validity checker is used by production recall path, not only by tests.

### `owl/stale_observation_guard.py`

Tasks:

- Keep current observation-id removal design.
- Ensure it trusts structured `file_path` first and only falls back to summary parsing when necessary.
- Optionally include stale reason variants:
  - fingerprint mismatch
  - file missing
  - unreadable file

Acceptance:

- Working memory observations are actually removed when their source file changes.

### `owl/semantic_memory.py`

Tasks:

- Make all file-backed records write and query against `file_path`, not only `repo_path`.
- Ensure `invalidate_by_file(file_path)` matches the real field being written.
- Consider convenience helper:
  - `invalidate_active_file_summaries(path, new_version)`

Acceptance:

- File invalidation API works against real stored records.

### `owl/memory_writer.py`

Tasks:

- When constructing file-backed semantic candidates, populate both:
  - `repo_path`
  - `file_path`
- Add invalidation intent output for writes.

Acceptance:

- Semantic records are file-addressable.

### `owl/memory_compactor.py`

Tasks:

- During promotion, write `file_path` for file summaries.
- Optionally supersede old records rather than only overwriting same `record_id`.

Acceptance:

- File summary lifecycle is explicit and validatable.

---

## P3. Prompt and Recall Source Unification

### `owl/context_manager.py`

Tasks:

- Replace `self.agent.memory_text()` in the `memory` section with a new working-memory-based render path.
- Remove prompt-time mixing of:
  - legacy `retrieval_candidates()`
  - new `MemoryRetriever.recall_for_task()`
- Render relevant memory from the new recall result only.
- Add richer metadata for each recalled item:
  - `source`
  - `record_id`
  - `repo_path`
  - `score`

Acceptance:

- Prompt assembly has one memory source of truth.

### `owl/working_memory.py`

Tasks:

- Improve render format if needed so prompt memory stays compact but useful.
- Consider adding explicit sections for:
  - recently touched files
  - pending verification
  - current plan

Acceptance:

- `WorkingMemory.render_text()` is good enough to replace legacy memory text in prompt assembly.

### `owl/memory_retriever.py`

Tasks:

- Ensure recall results carry enough metadata for prompt rendering and trace analysis.
- Decide whether working memory and semantic memory should be merged before or after ranking.
- Consider a two-stage approach:
  - working memory direct inclusion
  - semantic memory ranked recall

Acceptance:

- Relevant memory section is deterministic and explainable.

### `owl/context_builder.py`

Tasks:

- If this module remains in use, align its metadata structure with the new recall result format.
- Remove assumptions that relevant memory consists only of legacy note dicts.

Acceptance:

- Context-related code does not assume legacy note-only memory.

---

## P4. Retrieval Quality Improvements

### `owl/memory_utils.py`

Tasks:

- Replace whitespace-only tokenization with mixed tokenization:
  - regex word tokens
  - path segment split
  - snake_case split
  - camelCase split
- Keep behavior deterministic and testable.
- Revisit path extraction helpers to reduce accidental false matches.

Acceptance:

- Recall improves for code identifiers and file paths.

### `owl/semantic_memory.py`

Tasks:

- Align SQLite search behavior with in-memory search.
- Add support for `tags` filtering in SQLite mode.
- Ensure query filtering, ordering, and active-record filtering match fallback behavior.

Acceptance:

- SQLite and in-memory modes return similar results for the same query.

### `owl/recall_ranker.py`

Tasks:

- Revisit MMR behavior:
  - currently high-similarity items are downweighted, not strictly excluded
- Decide whether to implement true MMR selection or keep weighted diversity.
- Use file freshness/version signals if available, not only record `created_at`.
- Optionally include penalties for:
  - same file duplicates
  - same category duplicates

Acceptance:

- Recall is less redundant and more faithful to actual freshness.

### `owl/memory_config.py`

Tasks:

- Recalibrate weights after tokenizer and ranker changes.
- Add comments describing intended effects of each weight.

Acceptance:

- Recall scoring is easier to tune and reason about.

---

## P5. Legacy Layer Cleanup

### `owl/runtime.py`

Tasks:

- Remove runtime writes to legacy memory after the new stack is stable.
- Remove prompt dependencies on legacy memory APIs.

Acceptance:

- Runtime no longer depends on legacy memory for normal operation.

### `owl/memory.py`

Tasks:

- Reduce to migration/compatibility helpers, or remove entirely if no longer needed.
- If retained, document exact compatibility scope.

Acceptance:

- Legacy layer is either clearly isolated or deleted.

---

## 5. Test Plan by Phase

### P0 Tests

Files to update/add:

- `tests/test_memory_new_modules.py`
- `tests/test_pico.py`

Add coverage for:

- tool execution writes only working memory
- semantic records are written only during finalization/compaction
- no duplicate semantic write path remains

### P1 Tests

Files to update/add:

- `tests/test_run_store.py`
- `tests/test_memory_new_modules.py`
- new integration test for restart persistence

Add coverage for:

- semantic DB created under `.owl/memory/`
- records survive process recreation
- repo isolation

### P2 Tests

Files to update/add:

- `tests/test_memory_validity.py`
- `tests/test_pico.py`

Add coverage for:

- `read_file` records fingerprint
- `patch_file` invalidates old semantic summary
- stale observation removal happens in runtime flow
- recall skips stale semantic records

### P3 Tests

Files to update/add:

- `tests/test_context_manager.py`
- `tests/test_context_builder.py`

Add coverage for:

- prompt memory section comes from new working memory
- relevant memory does not mix duplicate legacy/new entries
- recall metadata appears in prompt metadata

### P4 Tests

Files to update/add:

- `tests/test_recall_ranker.py`
- `tests/test_memory_new_modules.py`
- new tokenizer tests

Add coverage for:

- path token matching
- snake/camel token matching
- SQLite/in-memory search parity

---

## 6. Recommended Implementation Order

Implement in this exact order:

1. `owl/runtime.py`
2. `owl/memory_writer.py`
3. `owl/memory_compactor.py`
4. `owl/semantic_memory.py`
5. `owl/memory_validity.py`
6. `owl/stale_observation_guard.py`
7. `owl/context_manager.py`
8. `owl/working_memory.py`
9. `owl/memory_retriever.py`
10. `owl/memory_utils.py`
11. `owl/recall_ranker.py`
12. legacy cleanup in `owl/memory.py`

Reason:

- Runtime defines the real execution path.
- Writer/compactor/semantic store form the core data pipeline.
- Validity and context should only be adjusted after the write/read lifecycle is correct.
- Retrieval quality tuning should happen after behavior is stable.

---

## 7. Minimal First Batch

If only one small but high-value refactor batch is feasible, do this:

1. Stop direct semantic writes in the tool loop.
2. Enable repo-scoped SQLite semantic persistence.
3. Wire fingerprint record/update/invalidate into runtime tool flow.
4. Stop mixing legacy relevant memory into prompt assembly.

This batch gives the highest architecture payoff with the lowest conceptual sprawl.

---

## 8. Deliverables

Expected deliverables after the full refactor:

- Runtime-aligned memory architecture
- Real cross-run semantic persistence
- Effective stale detection and invalidation
- Single-source prompt memory assembly
- Cleaner separation between short-term state and long-term reusable knowledge
- Lower maintenance cost for future memory work

