"""Custom model clients for failure-mode benchmark tasks.

These model clients deliberately trigger abnormal stop reasons:
- `RetryTriggeringModelClient` returns malformed outputs until retry limits hit.
- `ErrorInjectingModelClient` raises a model error from `complete()`.
"""

from __future__ import annotations

from .models import FakeModelClient


class RetryTriggeringModelClient(FakeModelClient):
    """Return malformed outputs that trigger retry_limit_reached.

    The Owl runtime parser treats these responses as retryable:

    - `<tool>` blocks with malformed JSON
    - empty `<final></final>` blocks
    - empty strings

    `FakeModelClient` falls back to a valid final answer when its output list is
    exhausted, so this client provides more outputs than the expected retry
    limit for failure-mode benchmarks.
    """

    def __init__(self):
        retry_responses = []
        templates = [
            '<tool>{bad json here</tool>',
            '<tool></tool>',
            '<final></final>',
            '<tool>not a dict</tool>',
            '<tool>""</tool>',
            '<tool>{name:}</tool>',
            '<final></final>',
            '',
        ]
        for i in range(32):
            retry_responses.append(templates[i % len(templates)])
        super().__init__(retry_responses)


class ErrorInjectingModelClient:
    """Raise `RuntimeError` from `complete()` to simulate backend failure."""

    def __init__(self):
        self.outputs: list[str] = []
        self.prompts: list[str] = []
        self.supports_prompt_cache = False
        self.last_completion_metadata: dict = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        del max_new_tokens, kwargs
        self.prompts.append(prompt)
        self.last_completion_metadata = {}
        raise RuntimeError("Simulated model backend failure")
