"""执行计划生成模块。

在主循环前调用模型生成结构化执行计划，实现"先规划再执行"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .memory_config import MAX_PLAN_STEPS


PLAN_PROMPT_TEMPLATE = """\
Analyze the task and produce a brief execution plan.
Respond ONLY in this format:

<plan>
steps:
- step: 1
  tool: <tool_name or none>
  rationale: <why>
  description: <what this step does>
</plan>

If the task is trivial and needs no tools, respond:
<plan>
no_action_needed: true
answer: <direct answer>
</plan>

Task: {task}
"""


@dataclass
class PlanStep:
    """执行计划中的单个步骤。"""

    step_number: int
    tool: str
    rationale: str
    description: str


@dataclass
class ExecutionPlan:
    """结构化执行计划。"""

    steps: list[PlanStep] = field(default_factory=list)
    no_action_needed: bool = False
    direct_answer: str = ""

    def render_for_context(self) -> str:
        """渲染为模型可读的紧凑上下文段落。"""
        if self.no_action_needed:
            return f"[Plan: no action needed. Direct answer: {self.direct_answer}]"
        if not self.steps:
            return ""
        lines = ["[Execution Plan]"]
        for s in self.steps[:MAX_PLAN_STEPS]:
            tool_label = s.tool if s.tool and s.tool != "none" else "analysis"
            lines.append(f"  {s.step_number}. [{tool_label}] {s.description} ({s.rationale})")
        return "\n".join(lines)

    @classmethod
    def parse_from_model_output(cls, raw: str) -> ExecutionPlan:
        """从模型输出中解析结构化计划。"""
        plan_match = re.search(r"<plan>(.*?)</plan>", raw, re.DOTALL)
        if not plan_match:
            return cls(steps=[])

        body = plan_match.group(1).strip()

        no_action_match = re.search(r"no_action_needed:\s*true", body, re.IGNORECASE)
        if no_action_match:
            answer_match = re.search(r"answer:\s*(.+)", body)
            return cls(
                no_action_needed=True,
                direct_answer=answer_match.group(1).strip() if answer_match else "",
            )

        steps: list[PlanStep] = []
        step_blocks = re.split(r"-\s*step:\s*\d+", body)
        step_nums = re.findall(r"-\s*step:\s*(\d+)", body)

        for i, block in enumerate(step_blocks[1:], start=0):
            if i >= len(step_nums):
                break
            tool_m = re.search(r"tool:\s*(.+)", block)
            rationale_m = re.search(r"rationale:\s*(.+)", block)
            desc_m = re.search(r"description:\s*(.+)", block)
            steps.append(PlanStep(
                step_number=int(step_nums[i]),
                tool=tool_m.group(1).strip() if tool_m else "none",
                rationale=rationale_m.group(1).strip() if rationale_m else "",
                description=desc_m.group(1).strip() if desc_m else "",
            ))

        return cls(steps=steps[:MAX_PLAN_STEPS])


def generate_plan(
    model_client,
    user_message: str,
    max_new_tokens: int,
    existing_context: str = "",
) -> ExecutionPlan:
    """调用模型生成执行计划。

    model_client: 需要有 complete(prompt, max_new_tokens) 方法。
    """
    prompt = PLAN_PROMPT_TEMPLATE.format(task=user_message)
    if existing_context:
        prompt = f"Existing context:\n{existing_context}\n\n{prompt}"

    raw = model_client.complete(prompt, max_new_tokens)
    return ExecutionPlan.parse_from_model_output(raw)
