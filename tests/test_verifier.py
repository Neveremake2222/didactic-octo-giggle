"""Verifier 模块的单元测试。"""

from owl.verifier import VerificationGate, VerificationResult


# ---------------------------------------------------------------------------
# VerificationResult.parse
# ---------------------------------------------------------------------------

def test_parse_progress_yes():
    raw = "<verify>progress: yes\nnotes: Good progress\n</verify>"
    result = VerificationResult.parse(raw)
    assert result.passed
    assert result.progress == "yes"
    assert result.notes == "Good progress"
    assert not result.is_final


def test_parse_progress_no():
    raw = "<verify>progress: no\nnotes: Not helpful\n</verify>"
    result = VerificationResult.parse(raw)
    assert not result.passed
    assert result.progress == "no"
    assert result.notes == "Not helpful"


def test_parse_progress_partial():
    raw = "<verify>progress: partial\nnotes: Some progress\n</verify>"
    result = VerificationResult.parse(raw, is_final=True)
    assert result.passed
    assert result.progress == "partial"
    assert result.is_final


def test_parse_malformed_returns_yes():
    raw = "some random output"
    result = VerificationResult.parse(raw)
    assert result.passed
    assert result.progress == "yes"


def test_parse_empty():
    result = VerificationResult.parse("")
    assert result.passed


# ---------------------------------------------------------------------------
# VerificationGate
# ---------------------------------------------------------------------------

def test_gate_tracks_consecutive_failures():
    gate = VerificationGate()

    r1 = VerificationResult(passed=False, progress="no", notes="failed")
    gate._record(r1)
    assert gate._consecutive_failures == 1
    assert not gate.should_stop

    r2 = VerificationResult(passed=False, progress="no", notes="failed again")
    gate._record(r2)
    assert gate._consecutive_failures == 2
    assert not gate.should_stop

    r3 = VerificationResult(passed=False, progress="no", notes="third")
    gate._record(r3)
    assert gate._consecutive_failures == 3
    assert gate.should_stop


def test_gate_resets_on_success():
    gate = VerificationGate()

    gate._consecutive_failures = 2
    r = VerificationResult(passed=True, progress="yes", notes="ok")
    gate._record(r)
    assert gate._consecutive_failures == 0
    assert not gate.should_stop


def test_gate_results_accumulated():
    gate = VerificationGate()
    r = VerificationResult(passed=True, progress="yes", notes="ok")
    gate._record(r)
    gate._record(VerificationResult(passed=False, progress="no", notes="fail"))
    assert len(gate.results) == 2


def test_gate_max_failures_configurable():
    gate = VerificationGate(max_failures=2)
    gate._consecutive_failures = 2
    assert gate.should_stop
    gate._consecutive_failures = 1
    assert not gate.should_stop


# ---------------------------------------------------------------------------
# FakeModelClient helper for integration tests
# ---------------------------------------------------------------------------

class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def complete(self, prompt, max_new_tokens, **kwargs):
        return self.outputs.pop(0)


def test_gate_verify_tool_result_makes_model_call():
    fake = FakeModelClient([
        "<verify>progress: yes\nnotes: Good\n</verify>",
    ])
    gate = VerificationGate()
    result = gate.verify_tool_result(
        fake, "Read the file", "", "read_file", "file content", 512,
    )
    assert result.passed
    assert result.progress == "yes"
    assert len(fake.outputs) == 0


def test_gate_verify_final_answer_makes_model_call():
    fake = FakeModelClient([
        "<verify>progress: yes\nnotes: Satisfied\n</verify>",
    ])
    gate = VerificationGate()
    result = gate.verify_final_answer(
        fake, "Summarize the file", "", "Here is the summary.", 512,
    )
    assert result.passed
    assert result.is_final
    assert result.notes == "Satisfied"


def test_gate_fails_open_on_model_error():
    class ErrorClient:
        def complete(self, *args, **kwargs):
            raise RuntimeError("model error")

    gate = VerificationGate()
    result = gate.verify_tool_result(
        ErrorClient(), "task", "", "read_file", "result", 512,
    )
    # Should fail open (assume ok)
    assert result.passed
    assert "failed" in result.notes
