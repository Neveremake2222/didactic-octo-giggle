"""Agent 运行时核心逻辑。

Owl 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import json
import os
import re
import textwrap
import uuid
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import memory as memorylib
from .context_manager import ContextManager
from .run_store import RunStore
from .task_state import TaskState
from . import tools as toolkit
from .workspace import MAX_HISTORY, WorkspaceContext, clip, now

# 新模块接入
from .execution_state import ExecutionState, PHASE_INITIALIZING, PHASE_PROMPT_BUILDING, PHASE_MODEL_CALLING, PHASE_TOOL_EXECUTING, PHASE_FINISHED
from .context_snapshot import ContextSnapshot
from .working_memory import WorkingMemory
from .semantic_memory import SemanticMemory
from .memory_writer import MemoryWriter
from .memory_retriever import MemoryRetriever
from .memory_compactor import MemoryCompactor

# 评估与观测模块
from .trace_schema import TraceEvent, parse_trace_file
from .trace_schema import (
    EVENT_CONTEXT_SOURCES_DISCOVERED,
    EVENT_PRECOMPACTION_FLUSHED,
    EVENT_CONTEXT_COMPACTED,
    EVENT_COMPACTION_PROMOTED,
    EVENT_MEMORY_SKIPPED_STALE,
    EVENT_MEMORY_RANKED,
    EVENT_MEMORY_DEDUPLICATED,
    EVENT_PROCEDURE_CANDIDATES_DETECTED,
)
from . import report_builder as _report_builder
from .failure_analyzer import classify_failure

# Phase 2 上下文发现模块
from .context_discovery import ContextDiscovery
from .context_invalidation import ContextInjectedTracker

# Phase 3 记忆有效性模块
from .memory_validity import FileFingerprintTracker, SemanticRecordValidityChecker
from .stale_observation_guard import StaleObservationGuard

# Phase 5 程序性经验检测
from .skill_candidate_registry import SkillCandidateRegistry

SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
REDACTED_VALUE = "<redacted>"
DEFAULT_SHELL_ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "PWD", "SHELL", "TERM", "TMPDIR", "TMP", "TEMP", "USER")
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "relevant_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
    "workspace_refresh": True,
    # Phase 2
    "context_discovery": True,
    "structured_compaction": True,
    "memory_validity": True,
    "stale_guard": True,
    "quality_recall": True,
    "procedure_detection": True,
}


@dataclass
class PromptPrefix:
    # prefix 除了文本本身，还带一小份元数据，
    # 这样 runtime 才能明确判断 prefix 是否可以复用。
    text: str
    hash: str
    workspace_fingerprint: str
    tool_signature: str
    built_at: str


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def save(self, session):
        path = self.path(session["id"])
        path.write_text(json.dumps(session, indent=2), encoding="utf-8")
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None


class Owl:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        run_store=None,
        approval_policy="ask",
        max_steps=6,
        max_new_tokens=512,
        depth=0,
        max_depth=1,
        read_only=False,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update({str(key): bool(value) for key, value in feature_flags.items()})
        self.run_store = run_store or RunStore(Path(workspace.repo_root) / ".owl" / "runs")
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": memorylib.default_memory_state(),
        }
        self.memory = memorylib.LayeredMemory(
            self.session.setdefault("memory", memorylib.default_memory_state()),
            workspace_root=self.root,
        )
        self.session["memory"] = self.memory.to_dict()
        self.tools = self.build_tools()
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text

        # --- 新模块：提前创建 MemoryRetriever 以注入 ContextManager ---
        self._init_memory_modules()

        self.session_path = self.session_store.save(self.session)
        self.current_task_state = None
        self.current_run_dir = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self._last_tool_result_metadata = {}
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }

        # --- 新模块：ExecutionState / WorkingMemory / SemanticMemory ---
        self.current_execution_state: ExecutionState | None = None
        self.working_memory = WorkingMemory()
        self.semantic_memory = SemanticMemory(db_path=self.semantic_memory_db_path())
        self._sync_context_manager_memory_sources()

        # --- Phase 2: 上下文发现 + 记忆有效性 ---
        self._context_injected_tracker: ContextInjectedTracker | None = None
        self._context_discovery: ContextDiscovery | None = None
        # 统一指纹追踪器（Phase 2/3 共用）
        self._fingerprint_tracker: FileFingerprintTracker | None = None

        # --- Phase 3: 记忆有效性 / Stale guard ---
        self._file_fingerprint_tracker: FileFingerprintTracker | None = None
        self._stale_observation_guard: StaleObservationGuard | None = None
        self._semantic_validity_checker: SemanticRecordValidityChecker | None = None

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    # -------------------------------------------------------------------------
    # 模块初始化子方法（P2-1: 拆分 God Class）
    # -------------------------------------------------------------------------

    def _init_memory_modules(self) -> None:
        """初始化记忆相关模块。

        包含 MemoryWriter、MemoryRetriever、MemoryCompactor，
        以及带 MemoryRetriever 注入的 ContextManager。
        """
        self._memory_writer = MemoryWriter()
        self._memory_retriever = MemoryRetriever()
        self._memory_compactor = MemoryCompactor()
        # Phase 2+: 注入 MemoryRetriever 使 ContextManager 能使用新召回系统
        self.context_manager = ContextManager(self, memory_retriever=self._memory_retriever)

    def _sync_context_manager_memory_sources(self) -> None:
        if not hasattr(self, "context_manager") or self.context_manager is None:
            return
        self.context_manager._memory_retriever = self._memory_retriever
        self.context_manager._working_memory = self.working_memory
        self.context_manager._semantic_memory = self.semantic_memory

    def _init_phase2_modules(self, workspace_root: str) -> None:
        """初始化 Phase 2 模块（每个 ask 独立实例）。

        包含：ContextInjectedTracker、ContextDiscovery、FileFingerprintTracker。
        """
        self._context_injected_tracker = ContextInjectedTracker()
        self._context_discovery = ContextDiscovery(workspace_root=workspace_root)
        # 统一指纹追踪器（Phase 2/3 共用，避免重复记录）
        self._fingerprint_tracker = FileFingerprintTracker()

    def _init_phase3_modules(self) -> None:
        """初始化 Phase 3 模块（复用 Phase 2 统一指纹追踪器）。

        包含：StaleObservationGuard。
        """
        # 优先复用 Phase 2 已创建的 tracker，避免重复记录
        if self._fingerprint_tracker is None:
            self._fingerprint_tracker = FileFingerprintTracker()
        # Phase 3 专属别名（向后兼容）
        self._file_fingerprint_tracker = self._fingerprint_tracker
        self._stale_observation_guard = StaleObservationGuard()
        self._semantic_validity_checker = SemanticRecordValidityChecker(workspace_root=str(self.root))
        self._memory_retriever.fingerprint_tracker = self._file_fingerprint_tracker
        self._memory_retriever.validity_checker = self._semantic_validity_checker

    def _finalize_success(
        self,
        task_state: TaskState,
        execution_state: ExecutionState,
        final: str,
        run_started_at: float,
        user_message: str,
    ) -> str:
        """ask() 成功路径的收尾逻辑。

        执行：状态切换 → 压缩沉淀 → 程序性经验检测 → trace/report → metrics。
        """
        execution_state.transition(PHASE_FINISHED)
        if self.feature_flags.get("structured_compaction"):
            compaction_report = self._memory_compactor.compact_and_promote_v2(
                self.working_memory, self.semantic_memory,
                task_state.run_id, user_message, str(self.root),
            )
            self.emit_trace(task_state, EVENT_PRECOMPACTION_FLUSHED,
                            {"schema": compaction_report.get("flush", {}).get("run_id", "")})
            self.emit_trace(task_state, EVENT_CONTEXT_COMPACTED,
                            compaction_report.get("compaction", {}))
            self.emit_trace(task_state, EVENT_COMPACTION_PROMOTED,
                            compaction_report.get("structured", {}))
        else:
            compaction_report = self._memory_compactor.compact_and_promote(
                self.working_memory, self.semantic_memory, str(self.root)
            )

        self._emit_compaction_trace(task_state, compaction_report)

        # Phase 5: 成功路径也检测程序性经验
        if self.feature_flags.get("procedure_detection"):
            self._detect_and_emit_procedure_candidates(task_state)

        self.run_store.write_task_state(task_state)
        self.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        self.run_store.write_report(task_state, self.redact_artifact(self.build_report(task_state)))
        self._save_metrics_json(task_state)
        return final

    def _finalize_stop(
        self,
        task_state: TaskState,
        execution_state: ExecutionState,
        final: str,
        run_started_at: float,
        user_message: str,
        reason: str,
    ) -> str:
        """ask() 停止路径（retry_limit / step_limit）的收尾逻辑。

        执行：标记状态 → 压缩沉淀 → 程序性经验检测 → trace/report → metrics。
        """
        if reason == "step_limit":
            task_state.stop_step_limit(final)
            execution_state.mark_stop("step_limit_reached")
        else:
            task_state.stop_retry_limit(final)
            execution_state.mark_stop("retry_limit_reached")

        if self.feature_flags.get("structured_compaction"):
            compaction_report = self._memory_compactor.compact_and_promote_v2(
                self.working_memory, self.semantic_memory,
                task_state.run_id, user_message, str(self.root),
            )
            self.emit_trace(task_state, EVENT_PRECOMPACTION_FLUSHED,
                            {"schema": compaction_report.get("flush", {}).get("run_id", "")})
            self.emit_trace(task_state, EVENT_CONTEXT_COMPACTED,
                            compaction_report.get("compaction", {}))
            self.emit_trace(task_state, EVENT_COMPACTION_PROMOTED,
                            compaction_report.get("structured", {}))
        else:
            compaction_report = self._memory_compactor.compact_and_promote(
                self.working_memory, self.semantic_memory, str(self.root)
            )

        self._emit_compaction_trace(task_state, compaction_report, phase="compaction_stopped")

        # Phase 5: 检测程序性经验候选
        if self.feature_flags.get("procedure_detection"):
            self._detect_and_emit_procedure_candidates(task_state)

        self.record({"role": "assistant", "content": final, "created_at": now()})
        self.run_store.write_task_state(task_state)
        self.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        self.run_store.write_report(task_state, self.redact_artifact(self.build_report(task_state)))
        self._save_metrics_json(task_state)
        return final

    def _emit_compaction_trace(self, task_state: TaskState, compaction_report: dict, phase: str = "compaction") -> None:
        """发出压缩沉淀 trace。"""
        self.emit_trace(
            task_state,
            "memory_written",
            {
                "phase": phase,
                "memory_written_working": False,
                "memory_promoted_semantic": True,
                "compaction": compaction_report.get("compaction", {}),
                "promotion": compaction_report.get("promotion", {}),
            },
        )

    def _detect_and_emit_procedure_candidates(self, task_state: TaskState) -> None:
        """检测并发出程序性经验候选 trace。"""
        if not hasattr(self, "_skill_registry") or self._skill_registry is None:
            self._skill_registry = SkillCandidateRegistry()
        candidates = self._memory_compactor.detect_procedure_candidates(
            self.working_memory, task_state.run_id, self._skill_registry
        )
        if candidates:
            self.emit_trace(
                task_state,
                EVENT_PROCEDURE_CANDIDATES_DETECTED,
                {"count": len(candidates), "types": [c.pattern_type for c in candidates]},
            )

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    def build_tools(self):
        return toolkit.build_tool_registry(self)

    def tool_signature(self):
        payload = []
        for name in sorted(self.tools):
            tool = self.tools[name]
            payload.append(
                {
                    "name": name,
                    "schema": tool["schema"],
                    "risky": tool["risky"],
                    "description": tool["description"],
                }
            )
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def build_prefix(self):
        tool_lines = []
        for name, tool in self.tools.items():
            fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
            risk = "approval required" if tool["risky"] else "safe"
            tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
        tool_text = "\n".join(tool_lines)
        examples = "\n".join(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
                '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
                '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
                '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
                "<final>Done.</final>",
            ]
        )
        # prefix 可以理解成 agent 的"工作手册"：
        # 它是谁、工具怎么调用、当前仓库是什么状态，都写在这里。
        text = textwrap.dedent(
            f"""\
            You are Mini-Coding-Agent (owl), a small local coding agent working inside a local repository.

            Rules:
            - Use tools instead of guessing about the workspace.
            - Return exactly one <tool>...</tool> or one <final>...</final>.
            - Tool calls must look like:
              <tool>{{"name":"tool_name","args":{{...}}}}</tool>
            - For write_file and patch_file with multi-line text, prefer XML style:
              <tool name="write_file" path="file.py"><content>...</content></tool>
            - Final answers must look like:
              <final>your answer</final>
            - Never invent tool results.
            - Keep answers concise and concrete.
            - If the user asks you to create or update a specific file and the path is clear, use write_file or patch_file instead of repeatedly listing files.
            - Before writing tests for existing code, read the implementation first.
            - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
            - New files should be complete and runnable, including obvious imports.
            - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
            - Required tool arguments must not be empty. Do not call read_file, write_file, patch_file, run_shell, or delegate with args={{}}.

            Tools:
            {tool_text}

            Valid response examples:
            {examples}

            {self.workspace.text()}
            """
        ).strip()
        return PromptPrefix(
            text=text,
            hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            workspace_fingerprint=self.workspace.fingerprint(),
            tool_signature=self.tool_signature(),
            built_at=now(),
        )

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_prefix(self, force=False):
        if not force and not self.feature_enabled("workspace_refresh") and getattr(self, "prefix_state", None) is not None:
            self._last_prefix_refresh = {
                "workspace_changed": False,
                "prefix_changed": False,
            }
            return dict(self._last_prefix_refresh)

        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", None)

        # 工作区事实相对稳定，所以这里按整体刷新；
        # 只有这些事实真的变化了，才重建完整 prefix。
        refreshed_workspace = WorkspaceContext.build(
            self.root,
            include_git_metadata=getattr(self.workspace, "include_git_metadata", True),
        )
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = force or refreshed_workspace_fingerprint != previous_workspace_fingerprint
        if workspace_changed:
            self.workspace = refreshed_workspace

        prefix_state = self.build_prefix() if workspace_changed or force or previous_hash is None else self.prefix_state
        prefix_changed = force or previous_hash != prefix_state.hash
        if prefix_changed:
            self._apply_prefix_state(prefix_state)

        self._last_prefix_refresh = {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
        }
        return dict(self._last_prefix_refresh)

    def memory_text(self):
        if hasattr(self, "working_memory") and isinstance(self.working_memory, WorkingMemory):
            if not self.working_memory.is_empty():
                return self.working_memory.render_text()
        return self.memory.render_memory_text()

    def history_text(self):
        history = self.session["history"]
        if not history:
            return "- empty"

        lines = []
        seen_reads = set()
        recent_start = max(0, len(history) - 6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)

            if item["role"] == "tool":
                limit = 900 if recent else 180
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], limit))
            else:
                limit = 900 if recent else 220
                lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def record(self, item):
        self.session["history"].append(item)
        self.session_path = self.session_store.save(self.session)

    @staticmethod
    def looks_sensitive_env_name(name):
        upper = str(name).upper()
        return any(upper == marker or upper.endswith(marker) or upper.endswith(f"_{marker}") for marker in SENSITIVE_ENV_NAME_MARKERS)

    def is_secret_env_name(self, name):
        upper = str(name).upper()
        return upper in self.secret_env_names or self.looks_sensitive_env_name(upper)

    def secret_env_items(self):
        items = [
            (name, value)
            for name, value in os.environ.items()
            if self.is_secret_env_name(name) and value
        ]
        items.sort(key=lambda item: item[0])
        return items

    def secret_env_summary(self):
        names = [name for name, _ in self.secret_env_items()]
        return {
            "secret_env_count": len(names),
            "secret_env_names": names,
        }

    def redact_text(self, text):
        text = str(text)
        for _, value in sorted(self.secret_env_items(), key=lambda item: len(item[1]), reverse=True):
            text = text.replace(value, REDACTED_VALUE)
        return text

    def redact_artifact(self, value, key=None):
        if key and self.is_secret_env_name(key):
            return REDACTED_VALUE
        if isinstance(value, dict):
            return {
                str(item_key): self.redact_artifact(item_value, key=item_key)
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [self.redact_artifact(item, key=key) for item in value]
        if isinstance(value, tuple):
            return [self.redact_artifact(item, key=key) for item in value]
        if isinstance(value, str):
            redacted = self.redact_text(value)
            return redacted
        return value

    def shell_env(self):
        env = {
            name: os.environ[name]
            for name in self.shell_env_allowlist
            if name in os.environ
        }
        env["PWD"] = str(self.root)
        if "PATH" not in env and os.environ.get("PATH"):
            env["PATH"] = os.environ["PATH"]
        return env

    def prompt_metadata(self, user_message, prompt):
        _, metadata = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        prompt, metadata = self.context_manager.build(user_message)
        # 这里把"这轮 prompt 是怎么拼出来的"连同缓存相关状态一起记下来，
        # 后面 trace/report 才能解释清楚：为什么这一轮 prefix 变了、缓存有没有命中。
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "workspace_chars": len(self.workspace.text()),
                "memory_chars": len(self.memory_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
            }
        )
        metadata.update(self.secret_env_summary())
        return prompt, metadata

    def semantic_memory_db_path(self):
        return str(self.root / ".owl" / "memory" / "semantic-memory.db")

    def normalize_memory_path(self, raw_path):
        return self.memory.canonical_path(raw_path)

    def _normalized_tool_args(self, args):
        normalized = dict(args or {})
        path = normalized.get("path")
        if not path:
            return normalized
        normalized["path"] = self.normalize_memory_path(path)
        try:
            normalized["absolute_path"] = str(self.path(path))
        except Exception:
            pass
        return normalized

    def _record_tool_file_fingerprint(self, name, args):
        if not self._file_fingerprint_tracker:
            return ""
        path = str(args.get("path", "")).strip()
        absolute_path = str(args.get("absolute_path", "")).strip()
        if not path or not absolute_path or name not in {"read_file", "write_file", "patch_file"}:
            return ""
        try:
            content = Path(absolute_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        return self._file_fingerprint_tracker.record(absolute_path, content, alias=path)

    def _invalidate_semantic_for_modified_file(self, name, args):
        if name not in {"write_file", "patch_file"}:
            return 0
        path = str(args.get("path", "")).strip()
        if not path:
            return 0
        new_version = self._record_tool_file_fingerprint(name, args)
        return self.semantic_memory.invalidate_by_file(path, new_version=new_version or None)

    def emit_trace(self, task_state, event, payload=None):
        payload = self.redact_artifact(payload or {})
        payload["event"] = event
        payload["created_at"] = now()
        # trace 是运行中的逐事件时间线，适合回答"这一轮 agent 到底做了什么"。
        self.run_store.append_trace(task_state, payload)
        return payload

    def update_memory_after_tool(self, name, args, result):
        """把少量高价值工具结果沉淀到 working memory。

        为什么存在：
        并不是每个工具结果都值得长期带进下一轮 prompt。完整结果已经进了
        `history`，这里只挑少量"下一轮大概率还会用到"的事实做提纯，
        例如最近读写过哪些文件、某个文件读出来的短摘要。

        输入 / 输出：
        - 输入：工具名 `name`、参数 `args`、执行结果 `result`
        - 输出：无显式返回值，副作用是更新 `self.memory`

        在 agent 链路里的位置：
        它发生在 `run_tool()` 真正执行完工具之后、下一轮 prompt 组装之前。
        也就是说：工具结果先进入完整历史，再由这个函数择优沉淀成轻量记忆。
        """
        if not self.feature_enabled("memory"):
            return
        path = args.get("path")
        if not path:
            return

        canonical_path = self.memory.canonical_path(path)
        # 不是所有工具结果都进入工作记忆。
        # 读文件会生成摘要；写文件/patch 会让旧摘要失效，因为它们可能过期了。
        if name in {"read_file", "write_file", "patch_file"}:
            self.memory.remember_file(canonical_path)
        if name == "read_file":
            summary = memorylib.summarize_read_result(result)
            self.memory.set_file_summary(canonical_path, summary)
            self.memory.append_note(summary, tags=(canonical_path,), source=canonical_path)
        elif name in {"write_file", "patch_file"}:
            self.memory.invalidate_file_summary(canonical_path)

    def note_tool(self, name, args, result):
        self.update_memory_after_tool(name, args, result)

    def ask(self, user_message):
        """执行一次完整的 agent 回合，直到产出最终答案或命中停止条件。

        为什么存在：
        `ask()` 是整个 runtime 的总调度器。它把"用户提一个请求"扩展成一条
        可持续推进的控制循环：记录会话、组 prompt、调用模型、执行工具、
        写 trace/report、更新状态，直到模型给出最终答案或系统主动停下。

        输入 / 输出：
        - 输入：`user_message`，即用户这一次的任务描述
        - 输出：字符串形式的最终回答；如果中途达到步数上限或重试上限，
          返回的是一条停止原因说明

        在 agent 链路里的位置：
        它是 CLI 和底层工具/模型之间的核心桥梁。CLI 收到用户输入后基本只做
        一件事：调用 `agent.ask()`。而 `ask()` 内部再去驱动 `ContextManager`
        组 prompt、`model_client.complete()` 调模型、`run_tool()` 执行动作。
        如果新人想理解 owl 是怎么"从一句话跑成一个 agent 流程"的，
        这里就是最关键的入口。
        """
        run_started_at = time.monotonic()
        self.memory.set_task_summary(user_message)
        self.record({"role": "user", "content": user_message, "created_at": now()})

        task_state = TaskState.create(run_id=self.new_run_id(), task_id=self.new_task_id(), user_request=user_message)
        self.current_task_state = task_state
        self.current_run_dir = self.run_store.start_run(task_state)

        # --- 新模块：创建 ExecutionState ---
        execution_state = ExecutionState.create(
            run_id=task_state.run_id,
            task_id=task_state.task_id,
            step_budget=self.max_steps,
        )
        self.current_execution_state = execution_state

        self.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )

        tool_steps = 0
        attempts = 0
        max_attempts = max(self.max_steps * 3, self.max_steps + 4)

        # 新模块：初始化 working memory
        self.working_memory = WorkingMemory()
        self.working_memory.set_task_summary(user_message)

        # Phase 2+: 注入最新 memory 实例到 ContextManager
        self.context_manager._working_memory = self.working_memory
        self.context_manager._semantic_memory = self.semantic_memory

        # Phase 2: 初始化上下文发现模块（每个 ask 独立实例）
        if self.feature_flags.get("context_discovery"):
            self._init_phase2_modules(str(self.root))

        # Phase 3: 初始化 stale guard（复用 Phase 2 的统一指纹追踪器）
        if self.feature_flags.get("stale_guard"):
            self._init_phase3_modules()

        # 新模块：发出初始状态转换 trace
        self.emit_trace(
            task_state,
            "state_transition",
            {
                "phase": PHASE_INITIALIZING,
                "step": 0,
                "execution_state": execution_state.to_dict(),
            },
        )

        # 这是 agent 的主循环，可以按"感知 -> 决策 -> 行动 -> 记录"来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while tool_steps < self.max_steps and attempts < max_attempts:
            attempts += 1
            task_state.record_attempt()
            self.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()

            # 新模块：状态转换 → prompt_building
            execution_state.transition(PHASE_PROMPT_BUILDING)
            self.emit_trace(
                task_state,
                "state_transition",
                {
                    "phase": PHASE_PROMPT_BUILDING,
                    "step": tool_steps,
                    "attempt": attempts,
                },
            )

            prompt, prompt_metadata = self._build_prompt_and_metadata(user_message)

            # 新模块：创建并发出 context_snapshot
            ctx_snapshot = ContextSnapshot.from_build_result(
                run_id=task_state.run_id,
                task_id=task_state.task_id,
                prompt=prompt,
                budget_chars=prompt_metadata.get("prompt_budget_chars", 0),
                metadata=prompt_metadata,
            )
            self.emit_trace(
                task_state,
                "context_built",
                ctx_snapshot.to_dict(),
            )

            # Phase 2: 上下文发现 — 从候选文件路径发现局部上下文来源
            if self.feature_flags.get("context_discovery") and self._context_discovery:
                targets = self.working_memory.candidate_targets
                if targets:
                    sources = self._context_discovery.discover_for_paths(targets)
                    fresh_sources = [
                        s for s in sources
                        if self._context_injected_tracker.mark_injected(s)
                    ]
                    if fresh_sources:
                        local_context_text = self._context_discovery.render_for_prompt(fresh_sources)
                        prompt = self._context_discovery.inject_into_prompt(prompt, local_context_text)
                        self.emit_trace(
                            task_state,
                            EVENT_CONTEXT_SOURCES_DISCOVERED,
                            {"count": len(fresh_sources), "sources": [s.source_id for s in fresh_sources]},
                        )

            self.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )

            # 新模块：状态转换 → model_calling
            execution_state.transition(PHASE_MODEL_CALLING)
            self.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            prompt_cache_key = None
            prompt_cache_retention = None
            if getattr(self.model_client, "supports_prompt_cache", False):
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"
            model_started_at = time.monotonic()
            raw = self.model_client.complete(
                prompt,
                self.max_new_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
            completion_metadata = dict(getattr(self.model_client, "last_completion_metadata", {}) or {})
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 trace。
                prompt_metadata.update(completion_metadata)
            self.last_completion_metadata = completion_metadata
            self.last_prompt_metadata = prompt_metadata
            kind, payload = self.parse(raw)
            self.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )

            if kind == "tool":
                tool_steps += 1
                name = payload.get("name", "")
                args = payload.get("args", {})

                # 新模块：状态转换 → tool_executing
                execution_state.transition(PHASE_TOOL_EXECUTING)
                execution_state.record_tool_call(name)
                self.emit_trace(
                    task_state,
                    "state_transition",
                    {
                        "phase": PHASE_TOOL_EXECUTING,
                        "tool": name,
                        "step": tool_steps,
                    },
                )

                task_state.record_tool(name)
                tool_started_at = time.monotonic()
                result = self.run_tool(name, args)
                self.record(
                    {
                        "role": "tool",
                        "name": name,
                        "args": args,
                        "content": result,
                        "created_at": now(),
                    }
                )

                # 新模块：更新 working_memory 和 semantic_memory
                memory_args = self._normalized_tool_args(args)
                fingerprint = ""
                if name == "read_file":
                    fingerprint = self._record_tool_file_fingerprint(name, memory_args)
                write_decision = self._memory_writer.should_write(name, memory_args, result)
                self._memory_writer.write_working(self.working_memory, write_decision)
                invalidated_count = self._invalidate_semantic_for_modified_file(name, memory_args)
                if fingerprint:
                    self.emit_trace(
                        task_state,
                        "file_fingerprint_recorded",
                        {"tool": name, "path": memory_args.get("path", ""), "fingerprint": fingerprint},
                    )
                if invalidated_count:
                    self.emit_trace(
                        task_state,
                        "semantic_invalidated_by_file",
                        {"tool": name, "path": memory_args.get("path", ""), "invalidated_count": invalidated_count},
                    )
                self.emit_trace(
                    task_state,
                    "memory_written",
                    {
                        "target": write_decision.get("target", ""),
                        "category": write_decision.get("category", ""),
                        "tool": name,
                        "memory_written_working": write_decision.get("target", "") == "working",
                        "memory_promoted_semantic": False,
                        "fingerprint_recorded": bool(fingerprint),
                        "semantic_invalidated": invalidated_count,
                    },
                )

                # Phase 3: 检查 working memory 中的陈旧 observations
                if self.feature_flags.get("stale_guard") and self._stale_observation_guard:
                    stale = self._stale_observation_guard.check_working_memory(
                        self.working_memory, self._file_fingerprint_tracker
                    )
                    if stale:
                        removed = self._stale_observation_guard.remove_stale(
                            self.working_memory, stale
                        )
                        self.emit_trace(
                            task_state,
                            EVENT_MEMORY_SKIPPED_STALE,
                            {"removed": removed, "stale_sources": [s.file_path for s in stale]},
                        )
                        self.emit_trace(
                            task_state,
                            "stale_observations_removed",
                            {"removed": removed, "stale_sources": [s.file_path for s in stale]},
                        )

                execution_state.observe(clip(result, 200))

                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "tool_executed",
                    {
                        "name": name,
                        "args": args,
                        "result": clip(result, 500),
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                        **dict(self._last_tool_result_metadata or {}),
                    },
                )
                continue

            if kind == "retry":
                self.record({"role": "assistant", "content": payload, "created_at": now()})
                self.run_store.write_task_state(task_state)
                continue

            final = (payload or raw).strip()
            self.record({"role": "assistant", "content": final, "created_at": now()})
            task_state.finish_success(final)
            return self._finalize_success(
                task_state, execution_state, final, run_started_at, user_message
            )

        if attempts >= max_attempts and tool_steps < self.max_steps:
            reason = "retry_limit"
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
        else:
            reason = "step_limit"
            final = "Stopped after reaching the step limit without a final answer."

        return self._finalize_stop(
            task_state, execution_state, final, run_started_at, user_message, reason
        )

    def _save_metrics_json(self, task_state):
        """ask() 结束后自动保存 metrics.json。"""
        try:
            from .metrics import compute_metrics  # lazy import to avoid circular dependency
            trace_dicts = self.run_store.load_trace(task_state)
            trace_events = [TraceEvent.from_dict(e) for e in trace_dicts]
            metrics = compute_metrics(trace_events, task_state.to_dict())
            failure_cat = classify_failure(task_state.to_dict(), trace_dicts, metrics)
            if failure_cat:
                metrics["failure_category"] = failure_cat
            self.run_store.write_metrics(task_state, metrics)
        except Exception as exc:
            # metrics.json 是增强产物，不应阻断主流程，但需要可观测。
            self.emit_trace(task_state, "metrics_save_failed", {"error": repr(exc)})

    def run_tool(self, name, args):
        """执行一次工具调用，并在执行前后套上完整护栏。

        为什么存在：
        在 agent 系统里，真正危险的不是"模型会不会想调用工具"，而是
        "平台有没有在执行前把边界守住"。这个函数就是工具层的总闸口：
        所有工具调用都必须先经过它，不能让模型直接碰到底层函数。

        输入 / 输出：
        - 输入：工具名 `name`，参数字典 `args`
        - 输出：字符串结果。无论是成功结果还是错误信息，都会统一返回文本，
          这样模型下一轮都能继续消费这份反馈。

        在 agent 链路里的位置：
        它位于 `ask()` 的"模型决定要调用工具"之后，是控制循环里真正把模型
        意图落到外部世界的一步。因此这里串起了几乎所有安全与可控设计：
        工具是否存在、参数是否合法、是否重复、是否需要审批、执行结果是否裁剪、
        是否需要回写记忆。
        """
        # 工具执行不是"直接调函数"，而是一条带护栏的流水线：
        # 工具是否存在 -> 参数是否合法 -> 是否重复调用 -> 是否通过审批
        # -> 真正执行 -> 更新记忆。
        tool = self.tools.get(name)
        if tool is None:
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "unknown_tool",
                "security_event_type": "",
            }
            return f"error: unknown tool '{name}'"
        try:
            self.validate_tool(name, args)
        except Exception as exc:
            example = self.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "invalid_arguments",
                "security_event_type": security_event_type,
            }
            return message
        if self.repeated_tool_call(name, args):
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "repeated_identical_call",
                "security_event_type": "",
            }
            return f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
        if tool["risky"] and not self.approve(name, args):
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "approval_denied",
                "security_event_type": "read_only_block" if self.read_only else "approval_denied",
            }
            return f"error: approval denied for {name}"
        try:
            result = clip(tool["run"](args))
            self.update_memory_after_tool(name, args, result)
            self._last_tool_result_metadata = {
                "tool_status": "ok",
                "tool_error_code": "",
                "security_event_type": "",
            }
            return result
        except Exception as exc:
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            self._last_tool_result_metadata = {
                "tool_status": "error",
                "tool_error_code": "tool_failed",
                "security_event_type": security_event_type,
            }
            return f"error: tool {name} failed: {exc}"

    def repeated_tool_call(self, name, args):
        # agent 很常见的一种坏循环，是在没有新信息的情况下反复发起同一调用。
        # 这里提前挡掉最简单的这种循环。
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        if len(tool_events) < 2:
            return False
        recent = tool_events[-2:]
        return all(item["name"] == name and item["args"] == args for item in recent)

    @staticmethod
    def new_task_id():
        return "task_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def build_report(self, task_state):
        # report 是一次运行的最终摘要；
        # 和 trace 的区别在于，trace 关注过程，report 关注结果与关键指标。
        report = {
            "run_id": task_state.run_id,
            "task_id": task_state.task_id,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": task_state.final_answer,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "task_state": task_state.to_dict(),
            "prompt_metadata": self.last_prompt_metadata,
            "redacted_env": self.secret_env_summary(),
        }
        # 新模块：加入 execution_state 和 working_memory
        if self.current_execution_state:
            report["execution_state"] = self.current_execution_state.to_dict()
        if hasattr(self, "working_memory"):
            report["working_memory"] = self.working_memory.to_dict()
        if hasattr(self, "semantic_memory"):
            report["semantic_memory_summary"] = {
                "db_path": getattr(self.semantic_memory, "_db_path", ""),
                "record_count": self.semantic_memory.count(),
                "total_record_count": getattr(self.semantic_memory, "count_total", lambda: self.semantic_memory.count())(),
            }
        return report

    def tool_example(self, name):
        return toolkit.tool_example(name)

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        toolkit.validate_tool(self, name, args)
        if name == "delegate":
            if self.depth >= self.max_depth:
                raise ValueError("delegate depth exceeded")

    def tool_list_files(self, args):
        return toolkit.tool_list_files(self, args)

    def tool_read_file(self, args):
        return toolkit.tool_read_file(self, args)

    def tool_search(self, args):
        return toolkit.tool_search(self, args)

    def tool_run_shell(self, args):
        return toolkit.tool_run_shell(self, args)

    def tool_write_file(self, args):
        return toolkit.tool_write_file(self, args)

    def tool_patch_file(self, args):
        return toolkit.tool_patch_file(self, args)

    def tool_delegate(self, args):
        return toolkit.tool_delegate(self, args)

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        try:
            answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    @staticmethod
    def parse(raw):
        """把模型原始输出解析成 runtime 可执行的动作或最终答案。

        为什么存在：
        模型输出首先是自然语言文本，而 runtime 需要的是结构化决策：
        "这是工具调用"还是"这是最终答案"。如果没有这层解析，后面的工具校验、
        审批和执行链路就没法可靠工作。

        输入 / 输出：
        - 输入：模型返回的原始文本 `raw`
        - 输出：`(kind, payload)`，其中 `kind` 可能是 `tool`、`final`、`retry`

        在 agent 链路里的位置：
        它位于 `model_client.complete()` 之后、`run_tool()` 之前，是模型输出
        进入平台控制流的第一道结构化关口。
        """
        raw = str(raw)
        # 这里支持两种工具格式：
        # 1. <tool>...</tool> 里包 JSON，适合简短调用
        # 2. XML 风格属性/子标签，适合写文件这类多行内容
        if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
            body = Owl.extract(raw, "tool")
            try:
                payload = json.loads(body)
            except Exception:
                return "retry", Owl.retry_notice("model returned malformed tool JSON")
            if not isinstance(payload, dict):
                return "retry", Owl.retry_notice("tool payload must be a JSON object")
            if not str(payload.get("name", "")).strip():
                return "retry", Owl.retry_notice("tool payload is missing a tool name")
            args = payload.get("args", {})
            if args is None:
                payload["args"] = {}
            elif not isinstance(args, dict):
                return "retry", Owl.retry_notice()
            return "tool", payload
        if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
            payload = Owl.parse_xml_tool(raw)
            if payload is not None:
                return "tool", payload
            return "retry", Owl.retry_notice()
        if "<final>" in raw:
            final = Owl.extract(raw, "final").strip()
            if final:
                return "final", final
            return "retry", Owl.retry_notice("model returned an empty <final> answer")
        raw = raw.strip()
        if raw:
            return "final", raw
        return "retry", Owl.retry_notice("model returned an empty response")

    @staticmethod
    def retry_notice(problem=None):
        prefix = "Runtime notice"
        if problem:
            prefix += f": {problem}"
        else:
            prefix += ": model returned malformed tool output"
        return (
            f"{prefix}. Reply with a valid <tool> call or a non-empty <final> answer. "
            'For multi-line files, prefer <tool name="write_file" path="file.py"><content>...</content></tool>.'
        )

    @staticmethod
    def parse_xml_tool(raw):
        match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.S)
        if not match:
            return None
        attrs = Owl.parse_attrs(match.group("attrs"))
        name = str(attrs.pop("name", "")).strip()
        if not name:
            return None

        body = match.group("body")
        args = dict(attrs)
        for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
            if f"<{key}>" in body:
                args[key] = Owl.extract_raw(body, key)

        body_text = body.strip("\n")
        if name == "write_file" and "content" not in args and body_text:
            args["content"] = body_text
        if name == "delegate" and "task" not in args and body_text:
            args["task"] = body_text.strip()
        return {"name": name, "args": args}

    @staticmethod
    def parse_attrs(text):
        attrs = {}
        for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
            attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
        return attrs

    @staticmethod
    def extract(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:].strip()
        return text[start:end].strip()

    @staticmethod
    def extract_raw(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:]
        return text[start:end]

    def reset(self):
        self.session["history"] = []
        self.session["memory"].clear()
        self.session["memory"].update(memorylib.default_memory_state())
        self.memory = memorylib.LayeredMemory(self.session["memory"], workspace_root=self.root)
        self.session_store.save(self.session)

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved


MiniAgent = Owl
