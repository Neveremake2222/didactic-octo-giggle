"""Decision Gate 模块的单元测试。"""

from owl.decision_gate import (
    check_decision_gate,
    DecisionGateResult,
    DECISION_GATE_MODE_AUTO,
    DECISION_GATE_MODE_ASK,
    DECISION_GATE_MODE_ALWAYS,
)


# ---------------------------------------------------------------------------
# check_decision_gate - auto mode
# ---------------------------------------------------------------------------

def test_auto_always_approves():
    result = check_decision_gate(DECISION_GATE_MODE_AUTO, "run_shell", {}, {"risky": True})
    assert result.action == "approve"


def test_auto_approves_safe_tool():
    result = check_decision_gate(DECISION_GATE_MODE_AUTO, "read_file", {}, {"risky": False})
    assert result.action == "approve"


# ---------------------------------------------------------------------------
# check_decision_gate - ask mode
# ---------------------------------------------------------------------------

def test_ask_approves_safe_tools_without_prompt():
    result = check_decision_gate(DECISION_GATE_MODE_ASK, "read_file", {"path": "a.txt"}, {"risky": False})
    assert result.action == "approve"


def test_ask_prompts_risky_tool():
    responses = ["y"]
    def fake_input(_):
        return responses.pop(0)
    result = check_decision_gate(
        DECISION_GATE_MODE_ASK, "run_shell",
        {"command": "echo hi"}, {"risky": True},
        input_func=fake_input,
    )
    assert result.action == "approve"


def test_ask_rejects_on_no():
    responses = ["n"]
    def fake_input(_):
        return responses.pop(0)
    result = check_decision_gate(
        DECISION_GATE_MODE_ASK, "write_file",
        {"path": "a.txt", "content": "hi"}, {"risky": True},
        input_func=fake_input,
    )
    assert result.action == "reject"


def test_ask_rejects_on_eof():
    def fake_input(_):
        raise EOFError
    result = check_decision_gate(
        DECISION_GATE_MODE_ASK, "run_shell", {"command": "rm -rf /"}, {"risky": True},
        input_func=fake_input,
    )
    assert result.action == "reject"


# ---------------------------------------------------------------------------
# check_decision_gate - always mode
# ---------------------------------------------------------------------------

def test_always_prompts_safe_tool():
    responses = ["y"]
    def fake_input(_):
        return responses.pop(0)
    result = check_decision_gate(
        DECISION_GATE_MODE_ALWAYS, "read_file", {}, {"risky": False},
        input_func=fake_input,
    )
    assert result.action == "approve"


def test_always_rejects_on_no():
    responses = ["no"]
    def fake_input(_):
        return responses.pop(0)
    result = check_decision_gate(
        DECISION_GATE_MODE_ALWAYS, "read_file", {}, {"risky": False},
        input_func=fake_input,
    )
    assert result.action == "reject"


# ---------------------------------------------------------------------------
# modify action
# ---------------------------------------------------------------------------

def test_modify_returns_modified_args():
    responses = ["m", '{"command": "echo hello"}']
    def fake_input(_):
        return responses.pop(0)
    result = check_decision_gate(
        DECISION_GATE_MODE_ALWAYS, "run_shell",
        {"command": "rm -rf /"}, {"risky": True},
        input_func=fake_input,
    )
    assert result.action == "modify"
    assert result.modified_args == {"command": "echo hello"}


def test_modify_invalid_json_rejects():
    responses = ["m", "not json", "n"]
    def fake_input(_):
        return responses.pop(0)
    result = check_decision_gate(
        DECISION_GATE_MODE_ALWAYS, "run_shell",
        {"command": "rm -rf /"}, {"risky": True},
        input_func=fake_input,
    )
    assert result.action == "reject"


# ---------------------------------------------------------------------------
# DecisionGateResult dataclass
# ---------------------------------------------------------------------------

def test_result_approve():
    result = DecisionGateResult(action="approve")
    assert result.action == "approve"
    assert result.modified_args is None


def test_result_modify():
    result = DecisionGateResult(action="modify", modified_args={"command": "echo"})
    assert result.action == "modify"
    assert result.modified_args == {"command": "echo"}
