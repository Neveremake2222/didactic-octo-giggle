"""验证门控模块。

提供两层验证：
  - per-tool: 工具执行后轻量验证"是否有进展"
  - final: 最终答案是否满足用户请求

验证失败连续 N 次后自动终止任务。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .memory_config import MAX_CONSECUTIVE_VERIFICATION_FAILURES


TOOL_VERIFICATION_PROMPT = """\
Verify tool execution progress.
Task: {task}
Plan: {plan}
Latest tool: {tool} -> result: {result}

Did this help? Respond ONLY:
<verify>progress: yes|no|partial
notes: <brief note></verify>"""

FINAL_VERIFICATION_PROMPT = """\
Verify task completion.
Task: {task}
Plan: {plan}
Final answer: {answer}

Did the result satisfy the request? Respond ONLY:
<verify>progress: yes|no|partial
notes: <brief note></verify>"""


@dataclass
class VerificationResult:
    """验证结果。"""

    passed: bool
    progress: str  # "yes" | "no" | "partial"
    notes: str
    suggestion: str = ""
    is_final: bool = False

    @classmethod
    def parse(cls, raw: str, is_final: bool = False) -> VerificationResult:
        """从模型输出解析验证结果。"""
        verify_match = re.search(r"<verify>(.*?)</verify>", raw, re.DOTALL)
        if not verify_match:
            return cls(passed=True, progress="yes", notes="parse fallback", is_final=is_final)

        body = verify_match.group(1).strip()
        progress_m = re.search(r"progress:\s*(yes|no|partial)", body, re.IGNORECASE)
        notes_m = re.search(r"notes:\s*(.+?)(?:\n|$)", body)

        progress = progress_m.group(1).lower() if progress_m else "yes"
        notes = notes_m.group(1).strip() if notes_m else ""
        passed = progress in ("yes", "partial")

        return cls(passed=passed, progress=progress, notes=notes, is_final=is_final)


class VerificationGate:
    """管理验证状态，追踪连续失败次数。"""

    def __init__(self, max_failures: int = MAX_CONSECUTIVE_VERIFICATION_FAILURES):
        self._consecutive_failures: int = 0
        self._results: list[VerificationResult] = []
        self._max_failures = max_failures

    @property
    def should_stop(self) -> bool:
        return self._consecutive_failures >= self._max_failures

    @property
    def results(self) -> list[VerificationResult]:
        return list(self._results)

    def _record(self, result: VerificationResult) -> None:
        self._results.append(result)
        if result.progress == "no":
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 0

    def verify_tool_result(
        self,
        model_client,
        user_message: str,
        plan_text: str,
        tool_name: str,
        tool_result: str,
        max_new_tokens: int,
    ) -> VerificationResult:
        """轻量验证：工具执行后是否有进展。"""
        prompt = TOOL_VERIFICATION_PROMPT.format(
            task=user_message[:200],
            plan=plan_text[:300] if plan_text else "(no plan)",
            tool=tool_name,
            result=tool_result[:500],
        )
        try:
            raw = model_client.complete(prompt, max_new_tokens)
            result = VerificationResult.parse(raw, is_final=False)
        except Exception:
            result = VerificationResult(passed=True, progress="yes", notes="verification call failed, assuming ok")

        self._record(result)
        return result

    def verify_final_answer(
        self,
        model_client,
        user_message: str,
        plan_text: str,
        final_answer: str,
        max_new_tokens: int,
    ) -> VerificationResult:
        """完整验证：最终答案是否满足请求。"""
        prompt = FINAL_VERIFICATION_PROMPT.format(
            task=user_message[:200],
            plan=plan_text[:300] if plan_text else "(no plan)",
            answer=final_answer[:500],
        )
        try:
            raw = model_client.complete(prompt, max_new_tokens)
            result = VerificationResult.parse(raw, is_final=True)
        except Exception:
            result = VerificationResult(passed=True, progress="yes", notes="verification call failed, assuming ok")

        self._record(result)
        return result
