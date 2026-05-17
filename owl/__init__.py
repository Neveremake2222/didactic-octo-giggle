from .cli import build_agent, build_arg_parser, build_welcome, main
from .models import AnthropicCompatibleModelClient, FakeModelClient, OllamaModelClient, OpenAICompatibleModelClient
from .runtime import MiniAgent, Owl, SessionStore
from .workspace import WorkspaceContext

__all__ = [
    "AnthropicCompatibleModelClient",
    "FakeModelClient",
    "Owl",
    "build_agent",
    "build_arg_parser",
    "build_welcome",
    "main",
    "MiniAgent",
    "OllamaModelClient",
    "OpenAICompatibleModelClient",
    "SessionStore",
    "SkillMemoryService",
    "TaskGraphState",
    "WorkspaceContext",
]


def __getattr__(name):
    if name == "SkillMemoryService":
        from .skill_memory_service import SkillMemoryService

        return SkillMemoryService
    if name == "TaskGraphState":
        from .task_graph_state import TaskGraphState

        return TaskGraphState
    raise AttributeError(name)
