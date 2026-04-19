"""人工决策闸门。

在模型输出与工具执行之间插入确认环节，支持批准/拒绝/修改三种操作。
"""

from __future__ import annotations

from dataclasses import dataclass

DECISION_GATE_MODE_AUTO = "auto"
DECISION_GATE_MODE_ASK = "ask"
DECISION_GATE_MODE_ALWAYS = "always"


@dataclass
class DecisionGateResult:
    """闸门判定结果。"""

    action: str  # "approve" | "reject" | "modify"
    modified_args: dict | None = None


def _prompt_user(tool_name: str, tool_args: dict, input_func) -> DecisionGateResult:
    """交互式提示用户确认工具调用。

    返回 approve / reject / modify。
    """
    args_str = " ".join(f"{k}={v!r}" for k, v in tool_args.items()) if tool_args else "(no args)"
    prompt = f"  Approve {tool_name} {args_str}? [y/n/m=modify]: "

    try:
        response = input_func(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return DecisionGateResult(action="reject")

    if response in ("y", "yes"):
        return DecisionGateResult(action="approve")
    if response in ("m", "modify"):
        try:
            new_args_str = input_func("  New args (JSON): ").strip()
        except (EOFError, KeyboardInterrupt):
            return DecisionGateResult(action="reject")
        if new_args_str:
            import json
            try:
                return DecisionGateResult(action="modify", modified_args=json.loads(new_args_str))
            except json.JSONDecodeError:
                return DecisionGateResult(action="reject")
        return DecisionGateResult(action="reject")
    return DecisionGateResult(action="reject")


def check_decision_gate(
    gate_mode: str,
    tool_name: str,
    tool_args: dict,
    tool_spec: dict,
    input_func=None,
) -> DecisionGateResult:
    """判定工具调用是否需要人工确认。

    gate_mode:
      - "auto": 一律批准
      - "ask": 仅 risky 工具需确认
      - "always": 所有工具都需确认

    tool_spec: 来自 tools.py 的工具定义 dict，含 "risky" 键。
    input_func: 可注入的 input 函数，默认为 builtins.input。
    """
    if input_func is None:
        import builtins
        input_func = builtins.input

    if gate_mode == DECISION_GATE_MODE_AUTO:
        return DecisionGateResult(action="approve")

    is_risky = tool_spec.get("risky", False)

    if gate_mode == DECISION_GATE_MODE_ASK:
        if not is_risky:
            return DecisionGateResult(action="approve")
        return _prompt_user(tool_name, tool_args, input_func)

    if gate_mode == DECISION_GATE_MODE_ALWAYS:
        return _prompt_user(tool_name, tool_args, input_func)

    return DecisionGateResult(action="approve")
