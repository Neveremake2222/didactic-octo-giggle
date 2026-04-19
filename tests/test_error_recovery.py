"""Error Recovery 模块的单元测试。"""

import pytest
from owl.error_recovery import (
    RetryOutcome,
    TOOL_ALTERNATIVES,
    retry_tool_execution,
    try_alternative_tool,
    retry_model_call,
    _adapt_args,
)
from owl.memory_config import MAX_TOOL_RETRIES, MAX_MODEL_RETRIES


# ---------------------------------------------------------------------------
# TOOL_ALTERNATIVES
# ---------------------------------------------------------------------------

def test_tool_alternatives_defined():
    assert "patch_file" in TOOL_ALTERNATIVES
    assert "read_file" in TOOL_ALTERNATIVES
    assert "write_file" in TOOL_ALTERNATIVES
    assert "run_shell" in TOOL_ALTERNATIVES


# ---------------------------------------------------------------------------
# retry_tool_execution
# ---------------------------------------------------------------------------

def test_retry_succeeds_immediately():
    def success_tool(name, args):
        return f"{name} result"
    outcome = retry_tool_execution(success_tool, "read_file", {"path": "a.txt"})
    assert outcome.success
    assert outcome.result == "read_file result"
    assert outcome.attempts == 1


def test_retry_succeeds_on_second_attempt():
    call_count = 0
    def flaky_tool(name, args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient error")
        return f"{name} result"
    outcome = retry_tool_execution(flaky_tool, "read_file", {"path": "a.txt"}, max_retries=2)
    assert outcome.success
    assert outcome.attempts == 2
    assert "transient error" in outcome.error_log[0]


def test_retry_exhausted_after_max_attempts():
    def always_fail(name, args):
        raise RuntimeError("permanent error")
    outcome = retry_tool_execution(always_fail, "read_file", {"path": "a.txt"}, max_retries=2)
    assert not outcome.success
    assert outcome.attempts == 3  # initial + 2 retries
    assert len(outcome.error_log) == 3


def test_retry_handles_error_string():
    call_count = 0
    def error_tool(name, args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "error: tool failed"
        return "success"
    outcome = retry_tool_execution(error_tool, "read_file", {"path": "a.txt"})
    assert outcome.success
    assert outcome.attempts == 2


# ---------------------------------------------------------------------------
# try_alternative_tool
# ---------------------------------------------------------------------------

def test_alternative_succeeds():
    call_log = []
    def mock_run(name, args):
        call_log.append(name)
        if name == "patch_file":
            raise ValueError("old_text not found")
        return f"{name} result"
    outcome = try_alternative_tool(mock_run, "patch_file", {"path": "a.txt"}, "old_text not found")
    assert outcome.success
    assert "write_file" in outcome.tried_alternatives


def test_alternative_fails_if_no_alternatives():
    def mock_run(name, args):
        raise ValueError("fail")
    outcome = try_alternative_tool(mock_run, "run_shell", {"command": "ls"}, "fail")
    assert not outcome.success
    assert outcome.tried_alternatives == []


def test_alternative_all_fail():
    def mock_run(name, args):
        raise ValueError("fail")
    outcome = try_alternative_tool(mock_run, "read_file", {"path": "a.txt"}, "fail")
    assert not outcome.success
    assert "run_shell" in outcome.tried_alternatives


# ---------------------------------------------------------------------------
# _adapt_args
# ---------------------------------------------------------------------------

def test_adapt_patch_to_write():
    alt = _adapt_args("patch_file", "write_file", {"path": "a.txt"}, "not found")
    assert alt is not None
    assert alt["path"] == "a.txt"
    assert "patch failed" in alt["content"]


def test_adapt_read_to_shell():
    alt = _adapt_args("read_file", "run_shell", {"path": "a.txt"}, "error")
    assert alt is not None
    assert "cat" in alt["command"]
    assert "a.txt" in alt["command"]


def test_adapt_returns_none_for_unknown():
    alt = _adapt_args("unknown_tool", "other", {}, "error")
    assert alt is None


# ---------------------------------------------------------------------------
# retry_model_call
# ---------------------------------------------------------------------------

def test_retry_model_succeeds_immediately():
    call_count = 0
    def good_model(prompt, max_tokens, **kw):
        nonlocal call_count
        call_count += 1
        return "<final>ok</final>"
    success, result = retry_model_call(good_model, "test", 512)
    assert success
    assert "<final>ok</final>" in result
    assert call_count == 1


def test_retry_model_succeeds_on_second_attempt():
    call_count = 0
    def flaky_model(prompt, max_tokens, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("server error")
        return "<final>ok</final>"
    success, result = retry_model_call(flaky_model, "test", 512)
    assert success
    assert call_count == 2


def test_retry_model_exhausted():
    def always_fail(prompt, max_tokens, **kw):
        raise RuntimeError("server error")
    success, result = retry_model_call(always_fail, "test", 512, max_retries=2)
    assert not success
    assert "after 2 attempts" in result
    assert "RuntimeError" in result or "error" in result.lower()


# ---------------------------------------------------------------------------
# RetryOutcome dataclass
# ---------------------------------------------------------------------------

def test_retry_outcome_fields():
    outcome = RetryOutcome(success=True, result="ok", attempts=1)
    assert outcome.success
    assert outcome.result == "ok"
    assert outcome.attempts == 1
    assert outcome.tried_alternatives == []
    assert outcome.error_log == []
