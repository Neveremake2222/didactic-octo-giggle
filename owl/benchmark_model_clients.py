"""Custom model clients for failure-mode benchmark tasks.

These model clients deliberately trigger abnormal stop reasons:
- RetryTriggeringModelClient: Returns malformed outputs to trigger retry_limit_reached
- ErrorInjectingModelClient: Raises RuntimeError to trigger model_error
"""

from __future__ import annotations

from .models import FakeModelClient


class RetryTriggeringModelClient(FakeModelClient):
    """Returns malformed outputs that trigger retry_limit_reached.

    The Owl runtime parse() method treats responses as follows:
    - Contains <tool> with malformed JSON → retry
    - Contains <final></final> (empty) → retry
    - Empty string → retry
    - Any other non-empty text → treated as final answer (NOT retry)

    FakeModelClient returns "<final>Done.</final>" when outputs list is empty,
    which is a valid final answer. We must provide enough entries so that
    max_attempts (max_steps * 3, here ~30) is reached before exhaustion.

    max_steps = 10 → max_attempts = 30. Provide 31+ entries.
    """

    def __init__(self):
        # Build a list of 32 retry-triggering responses (more than max_attempts)
        retry_responses = []
        # Mix of malformed JSON-in-tool, empty final, and empty strings
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
    """Raises RuntimeError on complete() to simulate a model backend failure.

    The runtime catches this and sets stop_reason = model_error.
    """

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
