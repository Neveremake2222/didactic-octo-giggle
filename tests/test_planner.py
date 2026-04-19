"""Planner 模块的单元测试。"""

from owl.planner import ExecutionPlan, PlanStep, generate_plan, PLAN_PROMPT_TEMPLATE


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        return self.outputs.pop(0)


# ---------------------------------------------------------------------------
# ExecutionPlan.parse_from_model_output
# ---------------------------------------------------------------------------

def test_parse_steps():
    raw = "<plan>steps:\n- step: 1\n  tool: read_file\n  rationale: read it\n  description: read the file\n- step: 2\n  tool: write_file\n  rationale: write it\n  description: write the file\n</plan>"
    plan = ExecutionPlan.parse_from_model_output(raw)
    assert len(plan.steps) == 2
    assert plan.steps[0].tool == "read_file"
    assert plan.steps[0].description == "read the file"
    assert plan.steps[1].step_number == 2
    assert not plan.no_action_needed


def test_parse_no_action_needed():
    raw = "<plan>no_action_needed: true\nanswer: The answer is 42.\n</plan>"
    plan = ExecutionPlan.parse_from_model_output(raw)
    assert plan.no_action_needed
    assert plan.direct_answer == "The answer is 42."
    assert plan.steps == []


def test_parse_malformed_returns_empty():
    raw = "Some random output without plan tags"
    plan = ExecutionPlan.parse_from_model_output(raw)
    assert plan.steps == []
    assert not plan.no_action_needed


def test_parse_empty_plan():
    raw = "<plan>\n</plan>"
    plan = ExecutionPlan.parse_from_model_output(raw)
    assert plan.steps == []
    assert not plan.no_action_needed


# ---------------------------------------------------------------------------
# ExecutionPlan.render_for_context
# ---------------------------------------------------------------------------

def test_render_with_steps():
    plan = ExecutionPlan(steps=[
        PlanStep(step_number=1, tool="read_file", rationale="check", description="read the file"),
        PlanStep(step_number=2, tool="write_file", rationale="update", description="write the file"),
    ])
    text = plan.render_for_context()
    assert "Execution Plan" in text
    assert "1." in text
    assert "read_file" in text
    assert "read the file" in text
    assert "check" in text


def test_render_no_action():
    plan = ExecutionPlan(no_action_needed=True, direct_answer="The answer is 42.")
    text = plan.render_for_context()
    assert "no action needed" in text
    assert "42" in text


def test_render_empty():
    plan = ExecutionPlan(steps=[])
    text = plan.render_for_context()
    assert text == ""


# ---------------------------------------------------------------------------
# generate_plan integration
# ---------------------------------------------------------------------------

def test_generate_plan_parses_and_stores():
    fake = FakeModelClient([
        "<plan>steps:\n- step: 1\n  tool: read_file\n  rationale: read\n  description: read the file\n</plan>",
    ])
    plan = generate_plan(fake, "Read the file", max_new_tokens=512)
    assert len(plan.steps) == 1
    assert plan.steps[0].tool == "read_file"
    assert len(fake.prompts) == 1
    assert "Read the file" in fake.prompts[0]


def test_generate_plan_no_action():
    fake = FakeModelClient([
        "<plan>no_action_needed: true\nanswer: Done.\n</plan>",
    ])
    plan = generate_plan(fake, "What is 2+2?", max_new_tokens=512)
    assert plan.no_action_needed
    assert plan.direct_answer == "Done."


def test_generate_plan_preserves_existing_context():
    fake = FakeModelClient([
        "<plan>steps:\n- step: 1\n  tool: search\n  rationale: find\n  description: search\n</plan>",
    ])
    generate_plan(fake, "Find files", max_new_tokens=512, existing_context="file: setup.py")
    assert "Existing context:" in fake.prompts[0]
    assert "setup.py" in fake.prompts[0]
