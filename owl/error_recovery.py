"""工具执行错误恢复机制。

提供重试、降级替代工具、模型调用重试三层恢复策略。
所有函数接收 callable 而非 Owl 实例，便于独立测试。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .memory_config import (
    BASE_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    MAX_MODEL_RETRIES,
    MAX_TOOL_RETRIES,
)


# 降级替代工具映射：主工具失败时可尝试的替代方案
TOOL_ALTERNATIVES: dict[str, list[str]] = {
    "patch_file": ["write_file"],
    "read_file": ["run_shell"],
    "search": ["run_shell"],
    "list_files": ["run_shell"],
    "write_file": [],
    "run_shell": [],
}


def _adapt_args(original_name: str, alt_name: str, original_args: dict, error_message: str) -> dict | None:
    """将原工具参数转换为替代工具参数。返回 None 表示无法适配。"""
    if original_name == "patch_file" and alt_name == "write_file":
        if "path" in original_args:
            return {"path": original_args["path"], "content": f"(patch failed: {error_message})"}
    if original_name == "read_file" and alt_name == "run_shell":
        if "path" in original_args:
            return {"command": f"cat {original_args['path']}"}
    if original_name == "search" and alt_name == "run_shell":
        if "pattern" in original_args:
            return {"command": f"grep -r '{original_args['pattern']}' ."}
    if original_name == "list_files" and alt_name == "run_shell":
        return {"command": "ls -la"}
    return None


@dataclass
class RetryOutcome:
    """重试/降级的结果摘要。"""

    success: bool
    result: str
    attempts: int
    tried_alternatives: list[str] = field(default_factory=list)
    error_log: list[str] = field(default_factory=list)


def retry_tool_execution(
    run_tool_func,
    tool_name: str,
    tool_args: dict,
    max_retries: int = MAX_TOOL_RETRIES,
) -> RetryOutcome:
    """重试失败的工具执行，带指数退避。

    run_tool_func: callable(name: str, args: dict) -> str
    """
    errors: list[str] = []
    for attempt in range(max_retries + 1):
        try:
            result = run_tool_func(tool_name, tool_args)
            if isinstance(result, str) and result.startswith("error:"):
                errors.append(f"attempt {attempt + 1}: {result}")
                if attempt < max_retries:
                    backoff = min(BASE_BACKOFF_SECONDS * (2 ** attempt), MAX_BACKOFF_SECONDS)
                    time.sleep(backoff)
                continue
            return RetryOutcome(success=True, result=result, attempts=attempt + 1, error_log=errors)
        except Exception as exc:
            errors.append(f"attempt {attempt + 1}: {exc!r}")
            if attempt < max_retries:
                backoff = min(BASE_BACKOFF_SECONDS * (2 ** attempt), MAX_BACKOFF_SECONDS)
                time.sleep(backoff)

    return RetryOutcome(
        success=False,
        result=f"error: {tool_name} failed after {max_retries + 1} attempts",
        attempts=max_retries + 1,
        error_log=errors,
    )


def try_alternative_tool(
    run_tool_func,
    original_name: str,
    original_args: dict,
    error_message: str,
) -> RetryOutcome:
    """尝试降级替代工具。"""
    alternatives = TOOL_ALTERNATIVES.get(original_name, [])
    tried: list[str] = []
    errors: list[str] = [f"primary failed: {error_message}"]

    for alt_name in alternatives:
        alt_args = _adapt_args(original_name, alt_name, original_args, error_message)
        if alt_args is None:
            continue
        tried.append(alt_name)
        try:
            result = run_tool_func(alt_name, alt_args)
            if isinstance(result, str) and result.startswith("error:"):
                errors.append(f"alternative {alt_name}: {result}")
                continue
            return RetryOutcome(
                success=True, result=result, attempts=1,
                tried_alternatives=tried, error_log=errors,
            )
        except Exception as exc:
            errors.append(f"alternative {alt_name}: {exc!r}")

    return RetryOutcome(
        success=False,
        result=f"error: all alternatives failed for {original_name}",
        attempts=len(tried),
        tried_alternatives=tried,
        error_log=errors,
    )


def retry_model_call(
    model_complete_func,
    prompt: str,
    max_new_tokens: int,
    max_retries: int = MAX_MODEL_RETRIES,
    **kwargs,
) -> tuple[bool, str]:
    """模型调用重试，带指数退避。

    model_complete_func: callable(prompt, max_new_tokens, **kw) -> str
    返回 (success, result_or_error_message)。
    """
    for attempt in range(max_retries):
        try:
            result = model_complete_func(prompt, max_new_tokens, **kwargs)
            return True, result
        except Exception as exc:
            if attempt < max_retries - 1:
                backoff = min(BASE_BACKOFF_SECONDS * (2 ** attempt), MAX_BACKOFF_SECONDS)
                time.sleep(backoff)
            else:
                return False, f"model call failed after {max_retries} attempts: {exc!r}"
    return False, "model call failed: unexpected loop exit"
