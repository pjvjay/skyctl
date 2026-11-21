"""skyctl — multi-account Bluesky automation library and CLI."""

from .agents import (
    AgentAction,
    AgentGroup,
    GroupConfig,
    Persona,
    Personality,
    load_agents_config,
)
from .client import BskyClient
from .llm import LLMError, get_provider, register_provider
from .manager import BskyManager
from .models import AccountConfig, PostResult, ScheduledPost
from .richtext import (
    MAX_GRAPHEMES,
    PostTooLongError,
    detect_facets,
    grapheme_len,
    split_thread,
)
from .scheduler import BskyScheduler, TickResult
from .settings import Settings, load_settings
from .tools import ToolContext, ToolPolicy, ToolRegistry, registry as tool_registry

__all__ = [
    "AccountConfig",
    "AgentAction",
    "AgentGroup",
    "BskyClient",
    "BskyManager",
    "BskyScheduler",
    "GroupConfig",
    "LLMError",
    "MAX_GRAPHEMES",
    "Persona",
    "Personality",
    "PostResult",
    "PostTooLongError",
    "ScheduledPost",
    "Settings",
    "TickResult",
    "ToolContext",
    "ToolPolicy",
    "ToolRegistry",
    "detect_facets",
    "get_provider",
    "grapheme_len",
    "load_agents_config",
    "load_settings",
    "register_provider",
    "tool_registry",
    "split_thread",
]
