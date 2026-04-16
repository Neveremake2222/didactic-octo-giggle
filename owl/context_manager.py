from __future__ import annotations

import json
from dataclasses import dataclass

from . import memory as memorylib
from .context_budget import (
    DEFAULT_REDUCTION_ORDER,
    DEFAULT_SECTION_BUDGETS,
    DEFAULT_SECTION_FLOORS,
    DEFAULT_TOTAL_BUDGET,
    _tail_clip,
)
from .context_builder import CURRENT_REQUEST_SECTION, SECTION_ORDER
from .memory_config import RELEVANT_MEMORY_LIMIT


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
        memory_retriever=None,
        working_memory=None,
        semantic_memory=None,
    ):
        self.agent = agent
        self.total_budget = int(total_budget)
        self.section_budgets = dict(DEFAULT_SECTION_BUDGETS)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)
        self._memory_retriever = memory_retriever
        self._working_memory = working_memory
        self._semantic_memory = semantic_memory

    def _memory_retriever_source(self):
        return self._memory_retriever or getattr(self.agent, "_memory_retriever", None)

    def _working_memory_source(self):
        return self._working_memory or getattr(self.agent, "working_memory", None)

    def _semantic_memory_source(self):
        return self._semantic_memory or getattr(self.agent, "semantic_memory", None)

    def _use_new_recall(self):
        return self._memory_retriever_source() is not None and self._working_memory_source() is not None

    @staticmethod
    def _recall_item_metadata(result):
        metadata = dict(getattr(result, "metadata", {}) or {})
        return {
            "source": getattr(result, "source", ""),
            "record_id": metadata.get("record_id", ""),
            "repo_path": getattr(result, "repo_path", "") or metadata.get("file_path", ""),
            "score": float(
                getattr(result, "combined_score", 0.0)
                or getattr(result, "relevance_score", 0.0)
                or 0.0
            ),
        }

    def build(self, user_message):
        user_message = str(user_message)
        self.section_floors = self._compute_section_floors()
        memory_enabled = True
        relevant_memory_enabled = True
        context_reduction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")

        memory_text = "Memory:\n- disabled"
        working_memory = self._working_memory_source()
        if memory_enabled:
            if working_memory is not None and not getattr(working_memory, "is_empty", lambda: True)():
                memory_text = working_memory.render_text()
            else:
                memory_text = str(self.agent.memory_text())

        section_texts = {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "memory": memory_text,
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }

        new_recall_results = []
        memory_retriever = self._memory_retriever_source()
        semantic_memory = self._semantic_memory_source()
        if self._use_new_recall() and relevant_memory_enabled:
            from .workspace import now as _now_ts

            try:
                new_recall_results = memory_retriever.recall_for_task(
                    task=user_message,
                    working_memory=working_memory,
                    semantic_memory=semantic_memory,
                    top_k=RELEVANT_MEMORY_LIMIT,
                    now_ts=_now_ts(),
                )
            except Exception:
                new_recall_results = []

        selected_notes = []
        can_use_legacy_recall = (
            memory_enabled
            and relevant_memory_enabled
            and hasattr(self.agent, "memory")
            and hasattr(self.agent.memory, "retrieval_candidates")
        )
        if can_use_legacy_recall:
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)
            if selected_notes and all(getattr(result, "source", "") == "working" for result in new_recall_results):
                # Do not let a trivial working-memory echo suppress legacy episodic recall.
                new_recall_results = []

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(
                section_texts,
                selected_notes=selected_notes,
                new_recall_results=new_recall_results,
            )
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_notes=selected_notes,
                new_recall_results=new_recall_results,
                user_message=user_message,
                section_texts=section_texts,
            )
            return prompt, metadata

        budgets = dict(self.section_budgets)
        rendered = self._render_sections(
            section_texts,
            budgets,
            selected_notes=selected_notes,
            new_recall_results=new_recall_results,
        )
        prompt = self._assemble_prompt(rendered)
        reduction_log = []

        while len(prompt) > self.total_budget:
            overflow = len(prompt) - self.total_budget
            reduced = False
            for section in self.reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                new_budget = max(floor, current_budget - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(
                    section_texts,
                    budgets,
                    selected_notes=selected_notes,
                    new_recall_results=new_recall_results,
                )
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break

        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            new_recall_results=new_recall_results,
            user_message=user_message,
            section_texts=section_texts,
        )
        return prompt, metadata

    def _render_sections_without_reduction(self, section_texts, selected_notes=None, new_recall_results=None):
        selected_notes = selected_notes or []
        new_recall_results = new_recall_results or []
        relevant_lines = ["Relevant memory:"]
        if selected_notes:
            relevant_lines.extend(f"- {note['text']}" for note in selected_notes)
        if new_recall_results:
            for result in new_recall_results:
                source_tag = f"[{result.source}]" if hasattr(result, "source") else ""
                relevant_lines.append(f"- {source_tag} {result.content[:200]}")
        if not selected_notes and not new_recall_results:
            relevant_lines.append("- none")
        relevant_raw = "\n".join(relevant_lines)
        history = list(getattr(self.agent, "session", {}).get("history", []))
        history_raw = self._raw_history_text(history)
        return {
            "prefix": SectionRender(
                raw=section_texts["prefix"],
                budget=len(section_texts["prefix"]),
                rendered=section_texts["prefix"],
                details={},
            ),
            "memory": SectionRender(
                raw=section_texts["memory"],
                budget=len(section_texts["memory"]),
                rendered=section_texts["memory"],
                details={},
            ),
            "relevant_memory": SectionRender(
                raw=relevant_raw,
                budget=len(relevant_raw),
                rendered=relevant_raw,
                details={
                    "selected_notes": [note["text"] for note in selected_notes],
                    "rendered_notes": [note["text"] for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    "note_budget": 0,
                    "new_recall_count": len(new_recall_results),
                    "new_recall_sources": [getattr(result, "source", "") for result in new_recall_results],
                    "items": [self._recall_item_metadata(result) for result in new_recall_results],
                },
            ),
            "history": SectionRender(
                raw=history_raw,
                budget=len(history_raw),
                rendered=history_raw,
                details={
                    "rendered_entries": [],
                    "older_entries_count": 0,
                    "collapsed_duplicate_reads": 0,
                    "reused_file_summary_count": 0,
                    "summarized_tool_count": 0,
                },
            ),
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    def _compute_section_floors(self):
        floors = {section: max(20, int(budget) // 4) for section, budget in self.section_budgets.items()}
        floors.update(self._section_floor_overrides)
        return floors

    def _render_sections(self, section_texts, budgets, selected_notes=None, new_recall_results=None):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                rendered[section] = self._render_relevant_memory(
                    selected_notes or [],
                    int(budget or 0),
                    new_recall_results or [],
                )
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0))
            else:
                raw = section_texts[section]
                rendered_text = _tail_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(
                    raw=raw,
                    budget=int(budget) if budget is not None else 0,
                    rendered=rendered_text,
                    details={},
                )
        return rendered

    def _render_relevant_memory(self, selected_notes, budget, new_recall_results=None):
        new_recall_results = new_recall_results or []
        header = "Relevant memory:"
        note_texts = [str(note.get("text", "")) for note in selected_notes if str(note.get("text", "")).strip()]
        recall_texts = []
        for result in new_recall_results:
            source_tag = f"[{result.source}]" if hasattr(result, "source") else ""
            recall_texts.append(f"{source_tag} {result.content[:200]}")

        all_texts = note_texts + recall_texts
        raw_lines = [header] + [f"- {text}" for text in all_texts]
        raw = "\n".join(raw_lines) if all_texts else "\n".join([header, "- none"])
        if not all_texts:
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=raw,
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0,
                    "new_recall_count": 0,
                    "new_recall_sources": [],
                    "items": [],
                },
            )

        per_note_budget = self._per_note_budget(budget, len(all_texts), header)
        rendered_notes = []
        while True:
            rendered_notes = [_tail_clip(text, per_note_budget) for text in all_texts]
            rendered = "\n".join([header] + [f"- {text}" for text in rendered_notes])
            if len(rendered) <= budget or per_note_budget <= 1:
                break
            per_note_budget -= 1

        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)
            rendered_notes = [rendered]

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": per_note_budget,
                "new_recall_count": len(recall_texts),
                "new_recall_sources": [getattr(result, "source", "") for result in new_recall_results],
                "items": [self._recall_item_metadata(result) for result in new_recall_results],
            },
        )

    def _per_note_budget(self, budget, note_count, header):
        if note_count <= 0:
            return 0
        overhead = len(header) + 3 * note_count
        usable = max(0, budget - overhead)
        return max(1, usable // note_count)

    def _render_history_section(self, budget):
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw = self._raw_history_text(history)
        if not history:
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered="Transcript:\n- empty",
                details={
                    "rendered_entries": [],
                    "older_entries_count": 0,
                    "collapsed_duplicate_reads": 0,
                    "reused_file_summary_count": 0,
                    "summarized_tool_count": 0,
                },
            )

        recent_window = 6
        recent_start = max(0, len(history) - recent_window)
        prepared_entries, history_details = self._prepare_history_entries(history, recent_start)
        rendered_entries = []

        for entry in reversed(prepared_entries):
            candidate_lines = list(entry["lines"])
            candidate_entries = candidate_lines + rendered_entries
            candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
            if len(candidate_rendered) <= budget:
                rendered_entries = candidate_entries
                continue

            if entry["recent"]:
                item = entry["item"]
                available = budget - len("Transcript:")
                if rendered_entries:
                    available -= sum(len(line) + 1 for line in rendered_entries)
                available = max(20, available - 1)
                candidate_lines = self._render_history_item(item, available)
                candidate_entries = candidate_lines + rendered_entries
                candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
                if len(candidate_rendered) <= budget:
                    rendered_entries = candidate_entries
            else:
                smaller_lines = [
                    _tail_clip(line, 20) if line.startswith("[") else _tail_clip(line, 40)
                    for line in candidate_lines
                ]
                smaller_entries = smaller_lines + rendered_entries
                smaller_rendered = "\n".join(["Transcript:", *smaller_entries])
                if len(smaller_rendered) <= budget:
                    rendered_entries = smaller_entries

        rendered = "\n".join(["Transcript:", *rendered_entries])
        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)

        history_details["rendered_entries"] = rendered_entries
        return SectionRender(raw=raw, budget=budget, rendered=rendered, details=history_details)

    def _prepare_history_entries(self, history, recent_start):
        file_summaries = {}
        if hasattr(self.agent, "memory"):
            file_summaries = dict(self.agent.memory.to_dict().get("file_summaries", {}))

        seen_read_paths = set()
        collapsed_duplicate_reads = 0
        reused_file_summary_count = 0
        summarized_tool_count = 0
        older_entries_count = 0
        prepared = []

        for index, item in enumerate(history):
            recent = index >= recent_start
            if recent:
                prepared.append(
                    {
                        "item": item,
                        "recent": True,
                        "lines": self._render_history_item(item, 900),
                    }
                )
                continue

            if item.get("role") == "tool" and item.get("name") == "read_file":
                path = str(item.get("args", {}).get("path", "")).strip()
                if path in seen_read_paths:
                    collapsed_duplicate_reads += 1
                    continue
                seen_read_paths.add(path)
                if file_summaries.get(path, {}).get("summary", ""):
                    reused_file_summary_count += 1
            elif item.get("role") == "tool":
                summarized_tool_count += 1

            older_entries_count += 1
            prepared.append(
                {
                    "item": item,
                    "recent": False,
                    "lines": self._render_older_history_item(item, file_summaries),
                }
            )

        return prepared, {
            "recent_window": 6,
            "recent_start": recent_start,
            "older_entries_count": older_entries_count,
            "collapsed_duplicate_reads": collapsed_duplicate_reads,
            "reused_file_summary_count": reused_file_summary_count,
            "summarized_tool_count": summarized_tool_count,
        }

    def _render_older_history_item(self, item, file_summaries):
        if item["role"] != "tool":
            return [f"[{item['role']}] {_tail_clip(item['content'], 60)}"]

        name = str(item.get("name", ""))
        args = dict(item.get("args", {}) or {})
        if name == "read_file":
            path = str(args.get("path", "")).strip() or "<unknown>"
            summary = file_summaries.get(path, {}).get("summary", "") or memorylib.summarize_read_result(item.get("content", ""))
            return [f"[tool:read_file] {path} -> {summary}"]

        return [self._summarize_tool_output(name, args, item.get("content", ""))]

    def _summarize_tool_output(self, name, args, content):
        label = str(args.get("command", "")).strip() or name
        lines = [line.strip() for line in str(content).splitlines() if line.strip()]
        summary = " | ".join(lines[:3]) if lines else "(empty)"
        return f"[tool:{name}] {label} -> {summary}"

    def _raw_history_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        lines = []
        for item in history:
            if item["role"] == "tool":
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(str(item["content"]))
            else:
                lines.append(f"[{item['role']}] {item['content']}")
        return "\n".join(["Transcript:", *lines])

    def _render_history_item(self, item, line_limit):
        if item["role"] == "tool":
            prefix = f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}"
            content = _tail_clip(item["content"], max(20, line_limit))
            return [prefix, content]
        return [f"[{item['role']}] {_tail_clip(item['content'], line_limit)}"]

    def _assemble_prompt(self, rendered):
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
                rendered["history"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
        ).strip()

    def _metadata(self, prompt, rendered, budgets, reduction_log, selected_notes, new_recall_results, user_message, section_texts):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
        }

        new_recall_results = new_recall_results or []
        history_details = dict(rendered["history"].details or {})
        return {
            "prompt_chars": len(prompt),
            "prompt_budget_chars": self.total_budget,
            "prompt_over_budget": len(prompt) > self.total_budget,
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [note["text"] for note in selected_notes],
                "raw_chars": rendered["relevant_memory"].raw_chars,
                "rendered_chars": rendered["relevant_memory"].rendered_chars,
                "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
                "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
                "new_recall_count": len(new_recall_results),
                "new_recall_sources": [getattr(result, "source", "") for result in new_recall_results],
                "items": list(rendered["relevant_memory"].details.get("items", [])),
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
                "rendered_entries": list(history_details.get("rendered_entries", [])),
                "older_entries_count": int(history_details.get("older_entries_count", 0)),
                "collapsed_duplicate_reads": int(history_details.get("collapsed_duplicate_reads", 0)),
                "reused_file_summary_count": int(history_details.get("reused_file_summary_count", 0)),
                "summarized_tool_count": int(history_details.get("summarized_tool_count", 0)),
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }
