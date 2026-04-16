"""两段式压缩 schema 定义。

compaction_schema 定义了 working memory → semantic memory 沉淀时必须保留的骨架。
在真正执行压缩之前，先用 pre_compaction_flush() 把高价值状态快照出来，
再由 structured_compaction() 按 schema 生成可写入长期记忆的结构化条目。

这样做的好处：
  - 不会因为"直接压"而丢失原始请求、最终目标、已完成工作等关键骨架
  - 长任务跨轮次时仍能保持上下文不漂移
  - 所有沉淀内容都经过 schema 约束，避免噪声混入长期记忆
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Schema 数据类
# ---------------------------------------------------------------------------


@dataclass
class CompactionSchema:
    """两段式压缩的中间产物。

    包含了一次 run 中所有值得跨轮次保留的信息骨架，
    由 pre_compaction_flush() 从 WorkingMemory 快照生成。

    Attributes:
        original_request:   用户原始请求
        final_goal:        最终目标（从 wm.plan 读取）
        completed_work:    已完成的工作列表
        remaining_tasks:   剩余任务列表（从 wm.pending_verifications 读取）
        must_not_do:       必须不做的约束
        files_modified:    修改过的文件列表
        files_observed:    观察过的文件列表
        hypotheses_tested: 测试过的假设
        validations_passed: 通过的验证
        validations_failed: 失败的验证
        summary_text:     本次 run 的自然语言总结
    """

    original_request: str = ""
    final_goal: str = ""
    completed_work: list[str] = field(default_factory=list)
    remaining_tasks: list[str] = field(default_factory=list)
    must_not_do: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    files_observed: list[str] = field(default_factory=list)
    hypotheses_tested: list[str] = field(default_factory=list)
    validations_passed: list[str] = field(default_factory=list)
    validations_failed: list[str] = field(default_factory=list)
    summary_text: str = ""

    created_at: str = ""
    run_id: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = _now_iso()

    # -------------------------------------------------------------------------
    # 序列化
    # -------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_request": self.original_request,
            "final_goal": self.final_goal,
            "completed_work": list(self.completed_work),
            "remaining_tasks": list(self.remaining_tasks),
            "must_not_do": list(self.must_not_do),
            "files_modified": list(self.files_modified),
            "files_observed": list(self.files_observed),
            "hypotheses_tested": list(self.hypotheses_tested),
            "validations_passed": list(self.validations_passed),
            "validations_failed": list(self.validations_failed),
            "summary_text": self.summary_text,
            "created_at": self.created_at,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompactionSchema:
        return cls(
            original_request=str(data.get("original_request", "")),
            final_goal=str(data.get("final_goal", "")),
            completed_work=list(data.get("completed_work", [])),
            remaining_tasks=list(data.get("remaining_tasks", [])),
            must_not_do=list(data.get("must_not_do", [])),
            files_modified=list(data.get("files_modified", [])),
            files_observed=list(data.get("files_observed", [])),
            hypotheses_tested=list(data.get("hypotheses_tested", [])),
            validations_passed=list(data.get("validations_passed", [])),
            validations_failed=list(data.get("validations_failed", [])),
            summary_text=str(data.get("summary_text", "")),
            created_at=str(data.get("created_at", "")),
            run_id=str(data.get("run_id", "")),
        )

    def is_meaningful(self) -> bool:
        """该 schema 是否包含实质内容。"""
        return bool(
            self.final_goal
            or self.completed_work
            or self.remaining_tasks
            or self.summary_text
        )


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

def build_schema_from_working_memory(
    wm: Any,
    run_id: str,
    original_request: str,
) -> CompactionSchema:
    """从 WorkingMemory 快照生成 CompactionSchema。

    纯函数：不修改 wm，不写入任何存储。
    所有字段从 wm 的当前状态读取。

    参数：
      wm               — 当前 WorkingMemory 实例
      run_id           — 唯一 run 标识
      original_request — 用户的原始请求（来自 ask() 入口参数）
    """
    # 从 observations 提取观察过的文件
    files_observed = []
    hypotheses_tested = []
    completed_work: list[str] = []

    for obs in wm.recent_observations:
        summary = getattr(obs, "summary", str(obs)) if hasattr(obs, "summary") else str(obs)
        tool = getattr(obs, "tool_name", "")
        # 文件路径提取（复用 memory_compactor 的逻辑）
        path = _extract_path_from_summary(summary)
        if path:
            files_observed.append(path)
        # 已完成的假设（含有完成/成功关键词）
        if any(kw in summary.lower() for kw in ("done", "success", "pass", "fixed", "updated")):
            completed_work.append(summary[:200])
        # 假设测试（含有 hypothesis 关键词）
        if "hypothesis" in summary.lower():
            hypotheses_tested.append(summary[:200])

    # 从 pending_verifications 提取剩余任务
    remaining_tasks = list(wm.pending_verifications)

    # 过滤重复
    files_observed = list(dict.fromkeys(files_observed))
    hypotheses_tested = list(dict.fromkeys(hypotheses_tested))
    completed_work = list(dict.fromkeys(completed_work))

    # 生成 summary_text
    summary_parts = []
    if wm.plan:
        summary_parts.append(f"Goal: {wm.plan}")
    if completed_work:
        summary_parts.append(f"Done: {', '.join(completed_work[:3])}")
    if remaining_tasks:
        summary_parts.append(f"Remaining: {', '.join(remaining_tasks[:3])}")
    summary_text = " | ".join(summary_parts)

    return CompactionSchema(
        original_request=original_request[:500],
        final_goal=wm.plan,
        completed_work=completed_work,
        remaining_tasks=remaining_tasks,
        files_observed=files_observed,
        hypotheses_tested=hypotheses_tested,
        summary_text=summary_text,
        run_id=run_id,
    )


def schema_to_semantic_records(
    schema: CompactionSchema,
) -> list[tuple[str, str, str, list[str]]]:
    """将 CompactionSchema 转换为可写入 SemanticMemory 的记录元组列表。

    返回格式：(record_id, category, content, tags)

    Categories:
      - "run_goal"         — 最终目标
      - "completed_work"   — 已完成工作
      - "remaining_tasks"  — 剩余任务
      - "must_not_do"      — 必须不做
      - "run_summary"      — run 自然语言总结
    """
    records: list[tuple[str, str, str, list[str]]] = []
    run_tag = f"run:{schema.run_id}"

    # run_goal
    if schema.final_goal:
        record_id = _make_record_id("run_goal", schema.run_id)
        records.append((
            record_id,
            "run_goal",
            f"Goal: {schema.final_goal}",
            ["run_goal", run_tag],
        ))

    # completed_work
    for i, work in enumerate(schema.completed_work):
        record_id = _make_record_id(f"completed:{schema.run_id}:{i}", work)
        records.append((
            record_id,
            "completed_work",
            work,
            ["completed_work", run_tag],
        ))

    # remaining_tasks
    for task in schema.remaining_tasks:
        record_id = _make_record_id(f"remaining:{schema.run_id}", task)
        records.append((
            record_id,
            "remaining_tasks",
            task,
            ["remaining_tasks", run_tag],
        ))

    # run_summary
    if schema.summary_text:
        record_id = _make_record_id("run_summary", schema.run_id)
        records.append((
            record_id,
            "run_summary",
            schema.summary_text,
            ["run_summary", run_tag],
        ))

    return records


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _extract_path_from_summary(summary: str) -> str:
    """从观察摘要中提取文件路径。"""
    # 格式: "read path/to/file: summary"
    if summary.startswith("read "):
        parts = summary.split(":", 1)
        path = parts[0].replace("read ", "").strip()
        if path:
            return path
    # 尝试找含 / 或文件后缀的词
    for word in summary.split():
        if "/" in word or word.endswith((".py", ".md", ".txt", ".json", ".yaml")):
            clean = word.strip("[]():,.")
            if clean:
                return clean
    return ""


def _make_record_id(category: str, key: str) -> str:
    """生成稳定的 record_id。"""
    raw = f"{category}:{key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
