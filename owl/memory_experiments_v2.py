"""L3 记忆专项实验。

扩展 metrics.py 中已有的记忆实验基础设施，新增：
  - noise_recall: 1 条相关 + 8 条无关记忆，验证召回准确率
  - conflict_resolution: 旧事实 vs 新事实，验证新事实优先
  - cross_session: 跨 session 记忆恢复

推荐指标：
  - correct_recall_rate: 正确召回比例
  - irrelevant_recall_rate: 召回无关记忆比例
  - stale_recall_rate: 错误使用旧知识比例
  - repeated_reads: 因记忆失败导致的重复读文件次数
"""

from __future__ import annotations

from typing import Any

from .metrics import (
    FakeModelClient,
    SessionStore,
    _build_experiment_workspace,
    _safe_mean,
    _safe_ratio,
    _temporary_workspace,
)
from .runtime import Owl


# ---------------------------------------------------------------------------
# Custom model clients for memory experiments
# ---------------------------------------------------------------------------

class _NoiseRecallModelClient(FakeModelClient):
    """Model client for noise recall experiment.

    Phase 1 (bootstrap): reads the fact file, then returns Done.
    Phase 2 (question): checks if the expected fact appears in prompt, answers accordingly.
    """

    def __init__(self, expected_fact: str, filename: str):
        super().__init__([])
        self.expected_fact = expected_fact.strip().lower()
        self.filename = filename
        self.phase = "bootstrap_tool"
        self.followup_reads = 0

    def complete(self, prompt, max_new_tokens, **kwargs):
        del max_new_tokens, kwargs
        self.prompts.append(prompt)
        self.last_completion_metadata = {}

        if self.phase == "bootstrap_tool":
            self.phase = "bootstrap_final"
            return f'<tool>{{"name":"read_file","args":{{"path":"{self.filename}","start":1,"end":20}}}}</tool>'

        if self.phase == "bootstrap_final":
            self.phase = "question"
            return "<final>Done.</final>"

        if self.phase == "question":
            prompt_lower = prompt.lower()
            if self.expected_fact in prompt_lower:
                return f"<final>{self.expected_fact.capitalize()}.</final>"
            self.phase = "question_after_read"
            self.followup_reads += 1
            return f'<tool>{{"name":"read_file","args":{{"path":"{self.filename}","start":1,"end":20}}}}</tool>'

        if self.phase == "question_after_read":
            self.phase = "done"
            return f"<final>{self.expected_fact.capitalize()}.</final>"

        return f"<final>{self.expected_fact.capitalize()}.</final>"


class _ConflictResolutionModelClient(FakeModelClient):
    """Model client for conflict resolution experiment.

    Phase 1: read file with old fact.
    Phase 2: read file with new fact.
    Phase 3: question that should be answered with the NEW fact.
    """

    def __init__(self, old_fact: str, new_fact: str, filename: str):
        super().__init__([])
        self.old_fact = old_fact.strip().lower()
        self.new_fact = new_fact.strip().lower()
        self.filename = filename
        self.phase = "bootstrap_old_tool"
        self.followup_reads = 0

    def complete(self, prompt, max_new_tokens, **kwargs):
        del max_new_tokens, kwargs
        self.prompts.append(prompt)
        self.last_completion_metadata = {}

        if self.phase == "bootstrap_old_tool":
            self.phase = "bootstrap_old_final"
            return f'<tool>{{"name":"read_file","args":{{"path":"{self.filename}","start":1,"end":20}}}}</tool>'

        if self.phase == "bootstrap_old_final":
            self.phase = "bootstrap_new_tool"
            return "<final>Done.</final>"

        if self.phase == "bootstrap_new_tool":
            self.phase = "bootstrap_new_final"
            return f'<tool>{{"name":"read_file","args":{{"path":"{self.filename}","start":1,"end":20}}}}</tool>'

        if self.phase == "bootstrap_new_final":
            self.phase = "question"
            return "<final>Done.</final>"

        if self.phase == "question":
            prompt_lower = prompt.lower()
            # Check if NEW fact is in prompt (correct)
            if self.new_fact in prompt_lower:
                return f"<final>{self.new_fact.capitalize()}.</final>"
            # Check if only OLD fact is in prompt (stale)
            self.followup_reads += 1
            self.phase = "question_after_read"
            return f'<tool>{{"name":"read_file","args":{{"path":"{self.filename}","start":1,"end":20}}}}</tool>'

        if self.phase == "question_after_read":
            return f"<final>{self.new_fact.capitalize()}.</final>"

        return f"<final>{self.new_fact.capitalize()}.</final>"


# ---------------------------------------------------------------------------
# Experiment A: Noise Recall
# ---------------------------------------------------------------------------

_NOISE_FACTS = [
    {"filename": "deploy_key.txt", "fact": "deploy key is AKIA-7749"},
]
_NOISE_IRRELEVANT = [
    "team mascot is blue",
    "lunch is at noon in room 3B",
    "parking lot is closed on weekends",
    "office thermostat is at 22 degrees",
    "standup is at 9:15 every morning",
    "coffee machine on floor 2 is broken",
    "budget review is next Thursday",
    "new hires start on Monday week 3",
]


def _run_noise_recall_variant(variant: str) -> dict[str, Any]:
    """Run one noise recall experiment iteration.

    Parameters
    ----------
    variant: "clean" (only relevant), "noisy" (1 relevant + 8 irrelevant), or "memory_off"
    """
    task = _NOISE_FACTS[0]
    with _temporary_workspace(prefix="owl-noise-recall-") as workspace_root:
        (workspace_root / task["filename"]).write_text(task["fact"] + "\n", encoding="utf-8")
        workspace = _build_experiment_workspace(workspace_root)
        store = SessionStore(workspace_root / ".owl" / "sessions")
        client = _NoiseRecallModelClient(task["fact"], task["filename"])
        agent = Owl(
            model_client=client,
            workspace=workspace,
            session_store=store,
            approval_policy="auto",
            feature_flags={"workspace_refresh": False},
        )

        # Bootstrap: read and remember the fact
        agent.ask(f"Read {task['filename']} and remember the key fact.")

        # Apply variant
        if variant == "memory_off":
            agent.feature_flags["memory"] = False
            agent.feature_flags["relevant_memory"] = False
        elif variant == "noisy":
            for idx, noise_text in enumerate(_NOISE_IRRELEVANT):
                agent.memory.append_note(
                    noise_text,
                    tags=("unrelated",),
                    created_at=f"2026-04-10T09:{idx:02d}:00+00:00",
                )
            agent.session["memory"] = agent.memory.to_dict()
            agent.session_store.save(agent.session)

        # Question: should recall the fact from memory
        result = agent.ask(f"What is the content of {task['filename']}?")
        task_state = agent.current_task_state

        return {
            "correct": task["fact"].lower() in result.strip().lower(),
            "repeated_reads": int(getattr(client, "followup_reads", 0)),
            "tool_steps": int(task_state.tool_steps),
        }


def run_noise_recall_experiment(repetitions: int = 3) -> dict[str, Any]:
    """Run noise recall experiment across variants and repetitions."""
    variants = {"clean": [], "noisy": [], "memory_off": []}
    for _ in range(int(repetitions)):
        for variant in variants:
            variants[variant].append(_run_noise_recall_variant(variant))

    return {
        "repetitions": int(repetitions),
        "variants": {
            name: {
                "correct_rate": _safe_ratio(sum(1 for r in rows if r["correct"]), len(rows)),
                "repeated_reads": sum(r["repeated_reads"] for r in rows),
                "avg_tool_steps": _safe_mean(r["tool_steps"] for r in rows),
            }
            for name, rows in variants.items()
        },
    }


# ---------------------------------------------------------------------------
# Experiment B: Conflict Resolution
# ---------------------------------------------------------------------------

def _run_conflict_resolution_variant() -> dict[str, Any]:
    """Run one conflict resolution experiment iteration.

    1. Write OLD fact to file, bootstrap agent reading it
    2. Write NEW fact to file, bootstrap agent reading it
    3. Ask question that should be answered with NEW fact
    """
    old_fact = "timeout is 30 seconds"
    new_fact = "timeout is 60 seconds"
    filename = "config.txt"

    with _temporary_workspace(prefix="owl-conflict-") as workspace_root:
        # Write OLD fact
        (workspace_root / filename).write_text(old_fact + "\n", encoding="utf-8")
        workspace = _build_experiment_workspace(workspace_root)
        store = SessionStore(workspace_root / ".owl" / "sessions")
        client = _ConflictResolutionModelClient(old_fact, new_fact, filename)
        agent = Owl(
            model_client=client,
            workspace=workspace,
            session_store=store,
            approval_policy="auto",
            feature_flags={"workspace_refresh": False},
        )

        # Bootstrap with old fact
        agent.ask(f"Read {filename} and remember the value.")

        # Overwrite file with new fact
        (workspace_root / filename).write_text(new_fact + "\n", encoding="utf-8")

        # Bootstrap with new fact
        agent.ask(f"Read {filename} again, the value has changed.")

        # Question: should answer with NEW fact
        result = agent.ask(f"What is the current timeout value from {filename}?")
        task_state = agent.current_task_state

        uses_new = new_fact.lower() in result.strip().lower()
        uses_old = old_fact.lower() in result.strip().lower() and not uses_new

        return {
            "correct": uses_new,
            "stale_recall": uses_old,
            "repeated_reads": int(getattr(client, "followup_reads", 0)),
            "tool_steps": int(task_state.tool_steps),
        }


def run_conflict_resolution_experiment(repetitions: int = 3) -> dict[str, Any]:
    """Run conflict resolution experiment."""
    rows = [_run_conflict_resolution_variant() for _ in range(int(repetitions))]

    return {
        "repetitions": int(repetitions),
        "correct_recall_rate": _safe_ratio(sum(1 for r in rows if r["correct"]), len(rows)),
        "stale_recall_rate": _safe_ratio(sum(1 for r in rows if r["stale_recall"]), len(rows)),
        "repeated_reads": sum(r["repeated_reads"] for r in rows),
        "avg_tool_steps": _safe_mean(r["tool_steps"] for r in rows),
    }


# ---------------------------------------------------------------------------
# Experiment C: Cross-Session Memory
# ---------------------------------------------------------------------------

def _run_cross_session_variant() -> dict[str, Any]:
    """Run one cross-session memory experiment iteration.

    Session 1: Read a file, remember a fact.
    Session 2: Load the session, recall the fact without re-reading.
    """
    fact = "api key is sk-test-1234"
    filename = "secrets.txt"

    with _temporary_workspace(prefix="owl-cross-session-") as workspace_root:
        (workspace_root / filename).write_text(fact + "\n", encoding="utf-8")
        workspace = _build_experiment_workspace(workspace_root)
        store = SessionStore(workspace_root / ".owl" / "sessions")

        # Session 1: read and remember
        client1 = _NoiseRecallModelClient(fact, filename)
        agent1 = Owl(
            model_client=client1,
            workspace=workspace,
            session_store=store,
            approval_policy="auto",
            feature_flags={"workspace_refresh": False},
        )
        agent1.ask(f"Read {filename} and remember the key fact.")
        session_id = agent1.session["id"]

        # Save session
        store.save(agent1.session)

        # Session 2: load session and recall
        client2 = _NoiseRecallModelClient(fact, filename)
        agent2 = Owl(
            model_client=client2,
            workspace=workspace,
            session_store=store,
            approval_policy="auto",
            feature_flags={"workspace_refresh": False},
        )
        # Load the previous session
        previous_session = store.load(session_id)
        if previous_session:
            agent2.session = previous_session
            # Restore memory state from session
            from . import memory as memorylib
            if "memory" in previous_session:
                agent2.memory.state = previous_session["memory"]

        result = agent2.ask(f"What was the key fact from {filename}?")
        task_state = agent2.current_task_state

        return {
            "correct": fact.lower() in result.strip().lower(),
            "repeated_reads": int(getattr(client2, "followup_reads", 0)),
            "tool_steps": int(task_state.tool_steps),
        }


def run_cross_session_experiment(repetitions: int = 3) -> dict[str, Any]:
    """Run cross-session memory experiment."""
    rows = [_run_cross_session_variant() for _ in range(int(repetitions))]

    return {
        "repetitions": int(repetitions),
        "correct_recall_rate": _safe_ratio(sum(1 for r in rows if r["correct"]), len(rows)),
        "repeated_reads": sum(r["repeated_reads"] for r in rows),
        "avg_tool_steps": _safe_mean(r["tool_steps"] for r in rows),
    }


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def run_memory_experiments_v2(repetitions: int = 3) -> dict[str, Any]:
    """Run all L3 memory experiments.

    Returns aggregate results for noise recall, conflict resolution, and cross-session.
    """
    return {
        "noise_recall": run_noise_recall_experiment(repetitions),
        "conflict_resolution": run_conflict_resolution_experiment(repetitions),
        "cross_session": run_cross_session_experiment(repetitions),
    }
