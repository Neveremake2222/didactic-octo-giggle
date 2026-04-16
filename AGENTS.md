# Repo Rules

## Development Standards

- All code changes should follow the existing patterns in the codebase.
- Write tests for new functionality; ensure existing tests pass before committing.
- Use `ruff` for linting: `ruff check .`
- Keep the module structure consistent with the existing design.

## Architecture Notes

- Memory system: WorkingMemory (per-run) → MemoryCompactor → SemanticMemory (cross-run)
- Context: assembled by ContextBuilder from WorkingMemory + SemanticMemory + ContextDiscovery
- All memory writes must go through MemoryWriter; all recalls must go through MemoryRetriever
